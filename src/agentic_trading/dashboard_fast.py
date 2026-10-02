"""The switchboard as the console sees it: named fields from ``data/state/fast.json``.

Only named fields pass through, so nothing the service writes later can reach
the page by accident. ``would_earn`` comes from the desk's latest weekly
allocation event, not from the service.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterable, Optional

COIN_FIELDS = ("symbol", "regime", "standing_aside")
TRADE_FIELDS = ("playbook", "entry", "stop", "target", "pnl_pct", "opened_at")
BOOK_FIELDS = ("equity", "return_pct", "entries", "exits")
MIRROR_FIELDS = ("equity", "return_pct", "unpriced")
RECENT_FIELDS = ("at", "event", "symbol", "text")
FRESH = timedelta(seconds=30)
NOTE = "the switchboard is not running: set [fast] enabled = true, then restart agentic-trading-venues"


def _pick(raw: Any, names: tuple[str, ...]) -> Optional[dict[str, Any]]:
    return {name: raw.get(name) for name in names} if isinstance(raw, dict) else None


def _would_earn(events: Iterable[dict[str, Any]]) -> Optional[float]:
    for record in reversed(list(events)):
        if record.get("event") == "desk_allocation" and isinstance(record.get("would_earn"), dict):
            try:
                return float(record["would_earn"].get("switchboard"))
            except (TypeError, ValueError):
                return None
    return None


def fast_view(state_dir: Path | str, events: Iterable[dict[str, Any]] = (), *,
              now: Optional[datetime] = None) -> dict[str, Any]:
    current = now or datetime.now(timezone.utc)
    try:
        data = json.loads((Path(state_dir) / "fast.json").read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            raise ValueError("not an object")
    except (OSError, ValueError):
        return {"enabled": False, "note": NOTE}
    try:
        as_of: Optional[datetime] = datetime.fromisoformat(str(data.get("as_of")))
    except ValueError:
        as_of = None
    if as_of is not None and as_of.tzinfo is None:
        as_of = None
    coins = []
    for raw in data.get("coins") or []:
        if isinstance(raw, dict):
            coin = _pick(raw, COIN_FIELDS) or {}
            coin["trade"] = _pick(raw.get("trade"), TRADE_FIELDS)
            coins.append(coin)
    recent = [_pick(r, RECENT_FIELDS) for r in (data.get("recent") or []) if isinstance(r, dict)][:10]
    return {
        "enabled": True,
        "as_of": data.get("as_of"),
        "stale": as_of is None or current - as_of > FRESH,
        "failed": str(data.get("failed") or ""),
        "halted": bool(data.get("halted")),
        "skipped": data.get("skipped"),
        "coins": coins,
        "book": _pick(data.get("book"), BOOK_FIELDS),
        "mirror": _pick(data.get("mirror"), MIRROR_FIELDS),
        "recent": recent,
        "would_earn": _would_earn(events),
    }
