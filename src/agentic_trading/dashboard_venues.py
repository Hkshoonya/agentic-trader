"""The venues service as the console sees it: whitelisted fields, nothing else.

Only named fields are passed through from ``state/venues.json``, so nothing
the service might write later (an account id, a key hint) can reach the page
by accident.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Optional

STREAM_FIELDS = ("key", "venue", "status", "connected", "last_tick_age_s", "delay_ms_median",
                 "delay_ms_p95", "reconnects", "skew", "last_error")
VENUE_FIELDS = ("name", "mode", "status", "equity", "cash", "day_trades", "armed", "last_error")
FRESH = timedelta(seconds=30)


def venues_view(state_dir: Path | str, *, now: Optional[datetime] = None) -> dict[str, Any]:
    current = now or datetime.now(timezone.utc)
    try:
        data = json.loads((Path(state_dir) / "venues.json").read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            raise ValueError("not an object")
    except (OSError, ValueError):
        return {"enabled": False,
                "note": "the venues service is not running (systemctl --user start agentic-trading-venues)"}
    try:
        as_of: Optional[datetime] = datetime.fromisoformat(str(data.get("as_of")))
    except ValueError:
        as_of = None

    def pick(rows: Any, names: tuple[str, ...]) -> list[dict[str, Any]]:
        return [{n: row.get(n) for n in names} for row in (rows or []) if isinstance(row, dict)]

    return {
        "enabled": True,
        "as_of": data.get("as_of"),
        "stale": as_of is None or current - as_of > FRESH,
        "streams": pick(data.get("streams"), STREAM_FIELDS),
        "venues": pick(data.get("venues"), VENUE_FIELDS),
    }
