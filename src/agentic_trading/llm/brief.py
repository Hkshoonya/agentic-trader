"""The daily brief: what the chat model is for here.

Jev answers narrow questions fast, many times over. The chat model is the
opposite tool: asked rarely, given a lot of state, and allowed to write prose.
So it is used for what only it can do — explaining the day to the operator.

The brief is **reporting only**. Nothing reads it back into the trading path, no
threshold consumes it, and a failure to produce one changes nothing. That is the
same rule the advisor follows: a model may reduce risk, and may explain, but may
never create a position or a size.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

BRIEF_FILE = "brief.json"

SYSTEM_PROMPT = (
    "You write a short operational brief for the operator of an automated "
    "trading agent. You are given the agent's own state: equity, promotion "
    "stage, risk budget, the book it wants, the gates that blocked it, the "
    "evidence behind its size, and what each agent in the fleet last did. "
    "Write 3-6 sentences. Say what the system did, what stopped it doing more, "
    "and anything that looks wrong. Do not give trading advice, do not predict "
    "prices, and do not invent numbers that are not in the state. If the "
    "evidence is thin, say so plainly."
)


def gather_state(config: Any, *, journal: Any = None) -> dict[str, Any]:
    """A compact, factual snapshot — only things already written to disk."""
    from agentic_trading.agents import load_roster

    state_dir = Path(config.state_dir)

    def read(name: str) -> dict[str, Any]:
        try:
            payload = json.loads((state_dir / name).read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}
        return payload if isinstance(payload, dict) else {}

    guard = read("risk_guard.json")
    promotion = read("promotion.json")
    limits = read("effective_limits.json")
    evidence = read("strategy_evidence.json")
    regime = read("regimes.json")
    entry_context = read("entry_context.json")
    production = (evidence.get("configs") or {}).get("production") or {}

    decisions: list[dict[str, Any]] = []
    try:
        records = list(journal.iter_today()) if journal is not None else []
    except Exception:  # noqa: BLE001 — a brief must not fail on a journal read
        records = []
    for record in records:
        if record.get("event") not in ("accepted", "rejected", "placed", "place_failed"):
            continue
        decisions.append(
            {
                "event": record.get("event"),
                "symbol": record.get("symbol") or (record.get("intent") or {}).get("symbol"),
                "reason": str(record.get("reason", ""))[:120],
                "notional": record.get("notional"),
            }
        )

    roster = load_roster(state_dir)
    return {
        "as_of": datetime.now(timezone.utc).isoformat(),
        "mode": read("live_gate.json").get("mode", ""),
        "stage": promotion.get("stage", ""),
        "streak": promotion.get("streak", 0),
        "equity": guard.get("current_equity", "0"),
        "kill_switch": guard.get("kill_switch", False),
        "per_order_pct": limits.get("max_order_pct", ""),
        "daily_notional_pct": limits.get("daily_notional_pct", ""),
        "evidence": {
            "trades": production.get("trades"),
            "expectancy_bps": production.get("expectancy_bps"),
            "max_drawdown_pct": production.get("max_drawdown_pct"),
            "generated_at": evidence.get("generated_at", ""),
        },
        "decisions_today": decisions[-12:],
        "decision_counts": {
            event: sum(1 for d in decisions if d["event"] == event)
            for event in ("accepted", "placed", "rejected", "place_failed")
        },
        "regimes": {
            symbol: {"regime": view.get("regime"), "blocks": view.get("blocks_entries")}
            for symbol, view in list((regime.get("views") or {}).items())[:16]
            if isinstance(view, dict)
        },
        "entry_judgments": {
            symbol: {k: v for k, v in view.items() if k in ("chase", "participation")}
            for symbol, view in (entry_context.get("views") or {}).items()
            if isinstance(view, dict)
        },
        "fleet": [
            {
                "agent": agent.get("name"),
                "health": (agent.get("health") or {}).get("status"),
                "last_error": (agent.get("health") or {}).get("last_error", ""),
            }
            for agent in roster.get("agents", [])
        ],
    }


def generate(config: Any, *, journal: Any = None, client: Any = None) -> Optional[dict[str, Any]]:
    """Ask the chat model for the brief; ``None`` when it is not configured."""
    from agentic_trading.llm.client import FakeLlmClient, build_llm_client

    resolved = client if client is not None else build_llm_client()
    if isinstance(resolved, FakeLlmClient):
        return None
    state = gather_state(config, journal=journal)
    started = datetime.now(timezone.utc)
    text = resolved.complete(
        SYSTEM_PROMPT, json.dumps(state, indent=2, default=str)
    ).strip()
    return {
        "generated_at": started.isoformat(),
        "model": getattr(resolved, "model", ""),
        "brief": text,
        "state": state,
    }


def write(config: Any, payload: dict[str, Any]) -> Path:
    from agentic_trading import jsonio

    path = Path(config.state_dir) / BRIEF_FILE
    jsonio.write_text(path, jsonio.dumps(payload, indent=2) + "\n")
    return path


def read(config: Any) -> Optional[dict[str, Any]]:
    try:
        payload = json.loads((Path(config.state_dir) / BRIEF_FILE).read_text())
    except (OSError, ValueError):
        return None
    return payload if isinstance(payload, dict) else None


def write_brief(config: Any, *, journal: Any = None, client: Any = None) -> Optional[Path]:
    """Produce and store one brief. Never raises for a model failure."""
    try:
        payload = generate(config, journal=journal, client=client)
    except Exception as exc:  # noqa: BLE001 — a brief is not worth an outage
        payload = {
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "error": f"{type(exc).__name__}: {exc}"[:300],
            "brief": "",
        }
    if payload is None:
        return None
    return write(config, payload)
