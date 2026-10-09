"""The swarm as the console sees it: named fields from ``data/state/swarm.json``.

Only named fields pass through, so nothing the step writes later (error
texts, signatures) can reach the page by accident. ``weight`` (the swarm is
funded) comes from the desk's latest weekly allocation event, not from the swarm.
"""

from __future__ import annotations

import json
import statistics
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterable, Optional

AGENT_FIELDS = ("id", "name", "family", "universe", "origin", "born", "forward_days", "excess_pct", "return_pct",
                "drawdown_pct", "state", "share")
RECENT_FIELDS = ("at", "event", "text")
BOOK_FIELDS = ("equity", "return_pct")
SCOUT_FIELDS = ("enabled", "spent", "budget", "last_rationale")
FRESH = timedelta(hours=36)
NOTE = ("the swarm has not run yet: set [swarm] enabled = true, then enable the agentic-trading-swarm timer")


def _pick(raw: Any, names: tuple[str, ...]) -> Optional[dict[str, Any]]:
    return {name: raw.get(name) for name in names} if isinstance(raw, dict) else None


def _weight(events: Iterable[dict[str, Any]]) -> Optional[float]:
    """The swarm's share of the desk's (simulated or live) account at the latest weekly allocation."""
    for record in reversed(list(events)):
        if record.get("event") == "desk_allocation" and isinstance(record.get("allocations"), dict):
            try:
                return float(record["allocations"].get("swarm"))
            except (TypeError, ValueError):
                return None
    return None


def _saved_desk(state_dir: Path) -> dict[str, Any]:
    try:
        saved = json.loads((state_dir / "desk" / "desk.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return saved if isinstance(saved, dict) else {}


def _current_weight(saved: dict[str, Any], events: Iterable[dict[str, Any]]) -> Optional[float]:
    """The desk's saved allocation (current, mid-week too); the latest weekly event if it can't be read."""
    try:
        return float(saved["allocations"]["swarm"])
    except (ValueError, KeyError, TypeError):
        return _weight(events)


def _follow(saved: dict[str, Any]) -> list[float]:
    """The desk's record of how far its follows sat from the swarm's paper prices, in bp."""
    raw = saved.get("follow")
    return [float(x) for x in raw if isinstance(x, (int, float))] if isinstance(raw, list) else []


def swarm_view(state_dir: Path | str, events: Iterable[dict[str, Any]] = (), *,
               now: Optional[datetime] = None) -> dict[str, Any]:
    current = now or datetime.now(timezone.utc)
    try:
        data = json.loads((Path(state_dir) / "swarm.json").read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            raise ValueError("not an object")
    except (OSError, ValueError):
        return {"enabled": False, "note": NOTE}
    try:
        stepped: Optional[datetime] = datetime.fromisoformat(str(data.get("stepped_at")))
    except ValueError:
        stepped = None
    if stepped is not None and stepped.tzinfo is None:
        stepped = None
    agents = [_pick(a, AGENT_FIELDS) for a in (data.get("agents") or []) if isinstance(a, dict)][:100]
    recent = [_pick(r, RECENT_FIELDS) for r in (data.get("recent") or []) if isinstance(r, dict)][:10]
    saved = _saved_desk(Path(state_dir))
    follow = _follow(saved)
    return {
        "enabled": True,
        "as_of": data.get("as_of"),
        "stale": bool(data.get("stale")) or stepped is None or current - stepped > FRESH,
        "note": str(data.get("note") or ""),
        "trials": data.get("trials"),
        "alive": data.get("alive"),
        "book": _pick(data.get("book"), BOOK_FIELDS),
        "agents": agents,
        "recent": recent,
        "scout": _pick(data.get("scout"), SCOUT_FIELDS),
        "weight": _current_weight(saved, events),
        "follow_gap_bps": statistics.median(follow) if follow else None,
        "follow_count": len(follow),
    }
