"""Arming a live venue: the one switch between paper and real money.

Only the ``venues arm`` command writes this file, for at most a day at a time.
Anything unexpected in it (unreadable JSON, a missing field, a naive time, a
window longer than a day) reads as disarmed: a live venue fails closed.
"""

from __future__ import annotations

import getpass
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Optional

from agentic_trading import jsonio

LIVE_VENUES = ("alpaca_live", "coinbase")
ARM_FILE = "venues_arm.json"
MAX_HOURS = 24.0


def _path(state_dir: Path | str) -> Path:
    return Path(state_dir) / ARM_FILE


def _read(state_dir: Path | str) -> dict[str, Any]:
    try:
        data = json.loads(_path(state_dir).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def _write(state_dir: Path | str, data: dict[str, Any]) -> None:
    Path(state_dir).mkdir(parents=True, exist_ok=True)
    jsonio.write_text(_path(state_dir), jsonio.dumps(data, indent=2) + "\n")


def arm(
    state_dir: Path | str, venue: str, *, hours: float, now: Optional[datetime] = None
) -> dict[str, str]:
    if venue not in LIVE_VENUES:
        raise ValueError(f"{venue} is not a live venue; paper venues need no arming")
    if not 0 < float(hours) <= MAX_HOURS:
        raise ValueError(f"arm for more than 0 and at most {MAX_HOURS:g} hours")
    current = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    try:
        who = getpass.getuser()
    except Exception:  # noqa: BLE001 - no login name (some containers): still arm
        who = "unknown"
    record = {
        "by": who,
        "armed_at": current.isoformat(),
        "until": (current + timedelta(hours=float(hours))).isoformat(),
    }
    data = _read(state_dir)
    data[venue] = record
    _write(state_dir, data)
    return record


def disarm(state_dir: Path | str, venue: str) -> None:
    data = _read(state_dir)
    data.pop(venue, None)
    _write(state_dir, data)


def is_armed(state_dir: Path | str, venue: str, *, now: datetime) -> bool:
    if venue not in LIVE_VENUES:
        return False
    record = _read(state_dir).get(venue)
    if not isinstance(record, dict):
        return False
    try:
        armed_at = datetime.fromisoformat(str(record["armed_at"]))
        until = datetime.fromisoformat(str(record["until"]))
    except (KeyError, TypeError, ValueError):
        return False
    if armed_at.tzinfo is None or until.tzinfo is None:
        return False
    if until - armed_at > timedelta(hours=MAX_HOURS) or until <= armed_at:
        return False
    return armed_at <= now < until
