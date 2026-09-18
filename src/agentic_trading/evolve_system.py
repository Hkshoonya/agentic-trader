"""The evolution agent: it proposes changes to the system, and cannot make them.

Everything else in this repository is bounded by the rule that a model may only
*reduce* risk. An agent that changes the system necessarily breaks that rule if
it can act, so this one is built the other way round: it reads the system's own
telemetry, writes concrete proposals with the evidence attached, and stops.

That is not a limitation worked around — it is the design that makes the agent
useful. A proposal is a reviewable artifact: a bounded description of the change,
the numbers that justify it, the risk of being wrong, and what would falsify it.
A human (or a coding agent acting on a human's instruction) implements it behind
review — the same branch protection that guards every other change here.

What it reads: the journal (what was decided and refused, and why), the
walk-forward report and its size frontier, measured execution costs, the agent
fleet's health, and the back-check results.

What it writes: `data/state/proposals.json` and one journal event. Nothing else.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Optional

PROPOSALS_FILE = "proposals.json"

# Fixed categories so proposals stay comparable over time, and so a reader can
# filter to the kind of change they are willing to review today.
CATEGORIES = (
    "data",          # feeds, history quality, coverage
    "research",      # evidence, sizing law, cost model
    "strategy",      # signals, gates, filters
    "execution",     # order construction, timing, broker behaviour
    "risk",          # limits, killers, exposure policy
    "reliability",   # failure modes, monitoring, restarts
    "performance",   # speed, cost, token budget
)

SYSTEM_PROMPT = (
    "You are the evolution agent for an automated trading system. You are given "
    "its own telemetry: decisions and refusals with reasons, the walk-forward "
    "evidence and its size frontier, measured execution costs, the health of each "
    "agent, and back-check results.\n\n"
    "Propose changes that would make the system more correct, more robust, or "
    "better evidenced. Rules:\n"
    "- A proposal must cite the numbers in the state that motivate it.\n"
    "- Never propose increasing risk, size, leverage or the number of positions "
    "without evidence that the measured drawdown still fits the 15% ceiling.\n"
    "- Every proposal must be implementable as a bounded code or config change, "
    "and must say what would falsify it.\n"
    "- If the telemetry shows nothing worth changing, return an empty list. Do "
    "not invent work.\n\n"
    'Return JSON only: {"proposals": [{"title": str, "category": one of '
    f"{list(CATEGORIES)}, \"evidence\": str, \"change\": str, "
    '"expected_effect": str, "risk": str, "falsified_if": str, '
    '"confidence": number 0-1}]}'
)


@dataclass
class Proposal:
    title: str
    category: str
    evidence: str
    change: str
    expected_effect: str
    risk: str
    falsified_if: str
    confidence: float
    status: str = "proposed"
    created_at: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "title": self.title,
            "category": self.category,
            "evidence": self.evidence,
            "change": self.change,
            "expected_effect": self.expected_effect,
            "risk": self.risk,
            "falsified_if": self.falsified_if,
            "confidence": round(self.confidence, 3),
            "status": self.status,
            "created_at": self.created_at,
        }


def _clip_text(value: Any, limit: int = 600) -> str:
    return str(value or "").strip()[:limit]


def parse_proposals(payload: Any, *, now: Optional[datetime] = None) -> list[Proposal]:
    """Read the model's answer, keeping only well-formed proposals.

    A proposal missing its evidence or its falsification test is dropped rather
    than stored: those two fields are what make it reviewable instead of an
    opinion, and a queue of opinions is worse than an empty queue.
    """
    if isinstance(payload, str):
        try:
            payload = json.loads(payload)
        except ValueError:
            return []
    if not isinstance(payload, dict):
        return []
    raw = payload.get("proposals")
    if not isinstance(raw, list):
        return []
    stamp = (now or datetime.now(timezone.utc)).isoformat()
    out: list[Proposal] = []
    for item in raw:
        if not isinstance(item, dict):
            continue
        category = str(item.get("category", "")).strip().lower()
        title = _clip_text(item.get("title"), 160)
        evidence = _clip_text(item.get("evidence"))
        change = _clip_text(item.get("change"))
        falsified = _clip_text(item.get("falsified_if"), 300)
        if not title or category not in CATEGORIES:
            continue
        if not evidence or not change or not falsified:
            continue
        try:
            confidence = float(item.get("confidence", 0.0))
        except (TypeError, ValueError):
            confidence = 0.0
        out.append(
            Proposal(
                title=title,
                category=category,
                evidence=evidence,
                change=change,
                expected_effect=_clip_text(item.get("expected_effect"), 300),
                risk=_clip_text(item.get("risk"), 300),
                falsified_if=falsified,
                confidence=min(1.0, max(0.0, confidence)),
                created_at=stamp,
            )
        )
    return out


def telemetry(config: Any, *, journal: Any = None) -> dict[str, Any]:
    """Everything the evolution agent is allowed to reason about."""
    from agentic_trading.agents import load_roster
    from agentic_trading.llm.brief import gather_state

    state = gather_state(config, journal=journal)
    state_dir = Path(config.state_dir)

    def read(name: str) -> dict[str, Any]:
        try:
            payload = json.loads((state_dir / name).read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}
        return payload if isinstance(payload, dict) else {}

    refusal_reasons: dict[str, int] = {}
    cycles = 0
    try:
        records: Iterable[dict[str, Any]] = list(journal.iter_today())
    except Exception:  # noqa: BLE001 — telemetry must not fail on a read
        records = []
    for record in records:
        event = record.get("event")
        if event == "rejected":
            reason = str(record.get("reason", "")).split(":")[0].strip()
            refusal_reasons[reason] = refusal_reasons.get(reason, 0) + 1
        elif event == "cycle_stats":
            cycles += int(record.get("cycles", 0) or 0)

    costs = read("execution_costs.json")
    health = read("health.json")
    evidence = read("strategy_evidence.json")
    return {
        **state,
        "refusals_today": refusal_reasons,
        "cycles_today": cycles,
        "execution_costs": {
            "measured_per_side_bps": costs.get("measured_per_side_bps"),
            "assumed_per_side_bps": costs.get("assumed_per_side_bps"),
            "usable": costs.get("usable"),
            "note": _clip_text(costs.get("note"), 200),
        },
        "size_frontier": evidence.get("size_frontier") or [],
        "back_check": {
            "healthy": health.get("healthy"),
            "warnings": [
                {"name": item.get("name"), "detail": _clip_text(item.get("detail"), 200)}
                for item in (health.get("warnings") or [])
            ],
            "failures": [
                {"name": item.get("name"), "detail": _clip_text(item.get("detail"), 200)}
                for item in (health.get("failures") or [])
            ],
        },
        "fleet_health": [
            {
                "agent": agent.get("name"),
                "health": (agent.get("health") or {}).get("status"),
                "failures": (agent.get("health") or {}).get("consecutive_failures"),
            }
            for agent in load_roster(config.state_dir).get("agents", [])
        ],
    }


def propose(
    config: Any, *, journal: Any = None, client: Any = None
) -> tuple[list[Proposal], str]:
    """Ask the chat model for proposals. ``([], "")`` when it is unavailable."""
    from agentic_trading.llm.client import FakeLlmClient, build_llm_client

    resolved = client if client is not None else build_llm_client()
    if isinstance(resolved, FakeLlmClient):
        return [], ""
    state = telemetry(config, journal=journal)
    reply = resolved.complete(SYSTEM_PROMPT, json.dumps(state, indent=2, default=str))
    return parse_proposals(reply), getattr(resolved, "model", "")


def write(config: Any, proposals: list[Proposal], *, model: str = "") -> Path:
    from agentic_trading import jsonio

    path = Path(config.state_dir) / PROPOSALS_FILE
    existing: list[dict[str, Any]] = []
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
        existing = list(payload.get("proposals") or [])
    except (OSError, ValueError):
        existing = []
    # Keep history: a proposal nobody implemented is itself a signal, and
    # re-proposing the same thing every day should read as such.
    titles = {str(item.get("title")) for item in existing}
    fresh = [p.to_dict() for p in proposals if p.title not in titles]
    jsonio.write_text(
        path,
        jsonio.dumps(
            {
                "updated_at": datetime.now(timezone.utc).isoformat(),
                "model": model,
                "proposals": existing + fresh,
            },
            indent=2,
        )
        + "\n",
    )
    return path


def read(config: Any) -> dict[str, Any]:
    try:
        payload = json.loads((Path(config.state_dir) / PROPOSALS_FILE).read_text())
    except (OSError, ValueError):
        return {}
    return payload if isinstance(payload, dict) else {}


def run(
    config: Any, *, journal: Any = None, client: Any = None
) -> list[Proposal]:
    """One evolution pass: propose, store, journal. It applies nothing."""
    try:
        proposals, model = propose(config, journal=journal, client=client)
    except Exception as exc:  # noqa: BLE001 — a failed pass changes nothing
        if journal is not None:
            journal.append(
                {
                    "event": "system_proposals_failed",
                    "error": f"{type(exc).__name__}: {exc}"[:200],
                }
            )
        return []
    if not proposals:
        return []
    write(config, proposals, model=model)
    if journal is not None:
        journal.append(
            {
                "event": "system_proposals",
                "count": len(proposals),
                "categories": sorted({p.category for p in proposals}),
                "titles": [p.title for p in proposals][:5],
            }
        )
    return proposals
