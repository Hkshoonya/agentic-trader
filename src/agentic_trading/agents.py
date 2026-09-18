"""The agents the system runs, what each one may do, and whether it is well.

The bot is not one loop. Data is fetched and repaired, research prices the rule
and refreshes the evidence, the strategy proposes, and execution is the only
thing that can reach the broker. Those are different jobs with different failure
modes, and treating them as one process hides exactly the thing an operator
needs to know when something is wrong.

What makes this a fleet rather than a label:

- **Authority is declared and ranked.** ``read_only`` < ``may_reduce_risk`` <
  ``may_trade``. Only the execution agent holds ``may_trade``; the research and
  data agents cannot touch the order path even by accident, and a test asserts
  it rather than trusting the call graph.
- **Health is derived from work done**, not from a hand-written string: the last
  success, the last attempt, consecutive failures and staleness against the
  agent's own cadence.
- **Failures are isolated.** One agent going quiet must not stop the others, and
  the roster is how that is observed rather than assumed.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

AGENTS_FILE = "agents.json"

# Ranked: a component may only do what its authority allows.
READ_ONLY = "read_only"
MAY_REDUCE_RISK = "may_reduce_risk"
MAY_TRADE = "may_trade"
AUTHORITY_ORDER = {READ_ONLY: 0, MAY_REDUCE_RISK: 1, MAY_TRADE: 2}


@dataclass(frozen=True)
class Agent:
    name: str
    role: str
    authority: str
    cadence_seconds: float
    description: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "role": self.role,
            "authority": self.authority,
            "cadence_seconds": self.cadence_seconds,
            "description": self.description,
        }


# The fleet. Cadence is how often the agent is *expected* to do something; the
# health rules below use it to decide when silence is a problem.
FLEET: tuple[Agent, ...] = (
    Agent(
        name="data",
        role="bars, quotes, correlations",
        authority=READ_ONLY,
        cadence_seconds=86_400.0,
        description=(
            "Refreshes daily bars from the broker and Coinbase, merges them, "
            "backfills volume and measures correlations. A data problem makes "
            "the others cautious, never reckless: the evidence gate refuses "
            "stale inputs."
        ),
    ),
    Agent(
        name="research",
        role="walk-forward evidence, execution costs",
        authority=READ_ONLY,
        cadence_seconds=604_800.0,
        description=(
            "Prices the fixed rule out-of-sample, records the size frontier and "
            "measures what fills actually cost. It can only ever *lower* the "
            "size the system is allowed to trade."
        ),
    ),
    Agent(
        name="strategy",
        role="trend signals + advisory gates",
        authority=MAY_REDUCE_RISK,
        cadence_seconds=3_600.0,
        description=(
            "The mechanical rule proposes; the LLM advisor and the regime gate "
            "may refuse an entry or block a regime. Neither can create a trade, "
            "size one up, or extend a hold."
        ),
    ),
    Agent(
        name="execution",
        role="risk guard, sizing, order placement",
        authority=MAY_TRADE,
        cadence_seconds=60.0,
        description=(
            "The only agent that can reach the broker, and only when every gate "
            "has passed *and* the operator has armed the session."
        ),
    ),
    Agent(
        name="evolution",
        role="proposes changes to the system",
        authority=READ_ONLY,
        cadence_seconds=86_400.0,
        description=(
            "Reads the system's own telemetry — refusals and their reasons, the "
            "evidence and its size frontier, measured costs, fleet health, "
            "back-check results — and writes reviewable proposals with the "
            "numbers attached. It cannot apply them: a model that may change the "
            "system is a model that may increase risk."
        ),
    ),
    Agent(
        name="backcheck",
        role="independent health checks",
        authority=READ_ONLY,
        cadence_seconds=1_800.0,
        description=(
            "Runs the six checks (state, data, analysis, evidence, broker, "
            "plumbing) and reports failures. It never repairs anything: a "
            "failure is a finding."
        ),
    ),
)

FLEET_BY_NAME = {agent.name: agent for agent in FLEET}


def authority_allows(authority: str, action: str) -> bool:
    """Whether an authority may perform an action.

    Used by tests and by the roster's own assertions, so "research cannot place
    orders" is a rule in one place rather than a property of the call graph.
    """
    required = {"read": READ_ONLY, "reduce_risk": MAY_REDUCE_RISK, "trade": MAY_TRADE}
    if action not in required:
        raise ValueError(f"unknown action: {action}")
    return AUTHORITY_ORDER.get(authority, -1) >= AUTHORITY_ORDER[required[action]]


def _age_seconds(stamp: Any, *, now: datetime) -> Optional[float]:
    try:
        seen = datetime.fromisoformat(str(stamp))
    except (TypeError, ValueError):
        return None
    if seen.tzinfo is None:
        seen = seen.replace(tzinfo=timezone.utc)
    return (now - seen).total_seconds()


@dataclass
class Health:
    status: str  # ok | stale | failing | unknown | disabled
    age_seconds: Optional[float] = None
    detail: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "age_seconds": (
                None if self.age_seconds is None else round(self.age_seconds, 1)
            ),
            **self.detail,
        }


def grade_health(
    entry: Optional[dict[str, Any]],
    *,
    now: Optional[datetime] = None,
    stale_after_factor: float = 3.0,
) -> Health:
    """Derive one agent's health from what it last did.

    ``stale`` means "no success within a few of its own cadences" — late enough
    to be a real problem, early enough to be worth telling someone about.
    """
    moment = now or datetime.now(timezone.utc)
    if not entry:
        return Health(status="unknown")
    if str(entry.get("status", "")) == "disabled":
        return Health(status="disabled")
    agent = FLEET_BY_NAME.get(str(entry.get("name", "")))
    cadence = float(
        entry.get("cadence_seconds")
        or (agent.cadence_seconds if agent else 3600.0)
    )
    failures = int(entry.get("consecutive_failures", 0) or 0)
    age = _age_seconds(entry.get("last_ok_at") or entry.get("last_run_at"), now=moment)
    detail: dict[str, Any] = {
        "last_ok_at": entry.get("last_ok_at", ""),
        "last_run_at": entry.get("last_run_at", ""),
        "consecutive_failures": failures,
        "last_error": str(entry.get("last_error", ""))[:200],
    }
    if failures >= 3:
        return Health(status="failing", age_seconds=age, detail=detail)
    if failures and detail["last_error"]:
        # It ran, it succeeded since, but the last recorded error is still
        # standing. Reporting that as plain "ok" is how a real defect hid in
        # plain sight: the data agent showed ok while its last_error named an
        # exception, and only a model reading the telemetry noticed.
        return Health(status="degraded", age_seconds=age, detail=detail)
    if age is None:
        return Health(status="unknown", age_seconds=None, detail=detail)
    if age > cadence * stale_after_factor:
        return Health(status="stale", age_seconds=age, detail=detail)
    return Health(status="ok", age_seconds=age, detail=detail)


def load_roster(state_dir: Path | str) -> dict[str, Any]:
    """The published roster with derived health, for the console and the CLI."""
    path = Path(state_dir) / AGENTS_FILE
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        payload = {}
    if not isinstance(payload, dict):
        payload = {}
    published = {
        str(entry.get("name")): entry
        for entry in (payload.get("agents") or [])
        if isinstance(entry, dict)
    }
    now = datetime.now(timezone.utc)
    roster: list[dict[str, Any]] = []
    for agent in FLEET:
        entry = dict(published.get(agent.name) or {})
        health = grade_health(entry or {"name": agent.name}, now=now)
        roster.append(
            {
                **agent.to_dict(),
                **{k: v for k, v in entry.items() if k not in {"name", "role"}},
                "health": health.to_dict(),
            }
        )
    return {
        "pid": payload.get("pid"),
        "mode": payload.get("mode", ""),
        "stage": payload.get("stage", ""),
        "updated_at": payload.get("updated_at", ""),
        "agents": roster,
    }
