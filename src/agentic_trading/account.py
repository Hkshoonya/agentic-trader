"""Runtime and money, measured from the first time the agent ever ran.

Two questions an operator asks constantly and the console could not answer:
*how long has this been running*, and *what has it actually done to the money*.

Both have to survive a restart, or the numbers reset exactly when they matter
most. So this keeps one durable record (`data/state/account.json`):

- **first equity ever seen**, so all-time P&L has a fixed origin instead of a
  baseline that moves every midnight;
- **total running seconds across every session**, plus the current session's
  start, so "session runtime" and "all-time runtime" are both real numbers;
- **the equity at each arming**, so "before arming" and "after arming" are two
  snapshots rather than a memory.

P&L is reported as *account equity now minus a snapshot*, and labelled with which
snapshot. Shadow and live fills are counted separately, because a number that
adds simulated fills to a real balance is worse than no number at all.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any, Optional

ACCOUNT_FILE = "account.json"


def _path(state_dir: Path | str) -> Path:
    return Path(state_dir) / ACCOUNT_FILE


def load(state_dir: Path | str) -> dict[str, Any]:
    try:
        payload = json.loads(_path(state_dir).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return payload if isinstance(payload, dict) else {}


def _save(state_dir: Path | str, payload: dict[str, Any]) -> None:
    from agentic_trading import jsonio

    jsonio.write_text(_path(state_dir), jsonio.dumps(payload, indent=2) + "\n")


def _decimal(value: Any) -> Optional[Decimal]:
    try:
        return Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError):
        return None


def _seconds_since(stamp: Any, *, now: Optional[datetime] = None) -> Optional[float]:
    try:
        seen = datetime.fromisoformat(str(stamp))
    except (TypeError, ValueError):
        return None
    if seen.tzinfo is None:
        seen = seen.replace(tzinfo=timezone.utc)
    return ((now or datetime.now(timezone.utc)) - seen).total_seconds()


def _between(start_stamp: Any, end_stamp: Any) -> float:
    """Seconds from one stamp to another, tolerating junk in either."""
    try:
        start = datetime.fromisoformat(str(start_stamp))
        end = datetime.fromisoformat(str(end_stamp))
    except (TypeError, ValueError):
        return 0.0
    if start.tzinfo is None:
        start = start.replace(tzinfo=timezone.utc)
    if end.tzinfo is None:
        end = end.replace(tzinfo=timezone.utc)
    return max(0.0, (end - start).total_seconds())


def note_session_start(
    state_dir: Path | str, *, now: Optional[datetime] = None
) -> dict[str, Any]:
    """Open a session, crediting the previous one's runtime to the total.

    A previous session that ended without a clean shutdown still counts up to its
    last heartbeat: the agent *was* running then, and pretending otherwise would
    understate the time it has been live.
    """
    moment = now or datetime.now(timezone.utc)
    payload = load(state_dir)
    previous_start = payload.get("session_started_at")
    last_beat = payload.get("last_heartbeat_at")
    total = float(payload.get("total_runtime_seconds", 0.0) or 0.0)
    if previous_start:
        # The previous session ran from its start until its last heartbeat; the
        # gap between that heartbeat and now is downtime, not runtime. Measuring
        # the other way round credited the outage to the agent.
        total += _between(previous_start, last_beat or previous_start)
    payload.update(
        {
            "session_started_at": moment.isoformat(),
            "last_heartbeat_at": moment.isoformat(),
            "total_runtime_seconds": round(total, 1),
            "sessions": int(payload.get("sessions", 0) or 0) + 1,
        }
    )
    _save(state_dir, payload)
    return payload


def note_equity(
    state_dir: Path | str, equity: Any, *, now: Optional[datetime] = None
) -> dict[str, Any]:
    """Record the equity now, and the very first one ever seen."""
    moment = now or datetime.now(timezone.utc)
    payload = load(state_dir)
    value = _decimal(equity)
    if value is None or value <= 0:
        return payload
    if "first_equity" not in payload:
        payload["first_equity"] = str(value)
        payload["first_seen_at"] = moment.isoformat()
    payload["last_equity"] = str(value)
    payload["last_equity_at"] = moment.isoformat()
    payload["last_heartbeat_at"] = moment.isoformat()
    _save(state_dir, payload)
    return payload


def note_armed(
    state_dir: Path | str,
    equity: Any,
    *,
    armed: bool,
    at: str = "",
    source: str = "",
) -> dict[str, Any]:
    """Snapshot the balance at the moment arming changes.

    Idempotent per arming: the console reads this on every refresh, and a
    snapshot that moves on every read would make "since arming" a lie.
    """
    payload = load(state_dir)
    stamp = str(at or datetime.now(timezone.utc).isoformat())
    if armed:
        if (
            payload.get("armed_at") != stamp
            or "equity_at_arm" not in payload
        ):
            payload.update(
                {
                    "armed": True,
                    "armed_at": stamp,
                    "equity_at_arm": str(equity),
                    "armed_source": source,
                }
            )
            _save(state_dir, payload)
    else:
        if payload.get("armed") or "armed_at" not in payload:
            payload.update(
                {
                    "armed": False,
                    "disarmed_at": stamp,
                }
            )
            _save(state_dir, payload)
    return payload


def note_heartbeat(
    state_dir: Path | str, *, now: Optional[datetime] = None
) -> None:
    """Cheap: stamp the current session so an unclean stop still counts."""
    payload = load(state_dir)
    payload["last_heartbeat_at"] = (now or datetime.now(timezone.utc)).isoformat()
    _save(state_dir, payload)


def totals(state_dir: Path | str, equity: Any, *, now: Optional[datetime] = None) -> dict[str, Any]:
    """Everything the console shows about runtime and money."""
    moment = now or datetime.now(timezone.utc)
    payload = load(state_dir)
    current = _decimal(equity)
    first = _decimal(payload.get("first_equity"))
    at_arm = _decimal(payload.get("equity_at_arm"))

    session_seconds = _seconds_since(payload.get("session_started_at"), now=moment)
    base_total = float(payload.get("total_runtime_seconds", 0.0) or 0.0)
    total_seconds = base_total + (session_seconds or 0.0)

    def delta(start: Optional[Decimal]) -> Optional[str]:
        if current is None or start is None:
            return None
        return str((current - start).quantize(Decimal("0.01")))

    return {
        "runtime": {
            "session_started_at": payload.get("session_started_at", ""),
            "session_seconds": None if session_seconds is None else round(session_seconds, 1),
            "total_seconds": round(total_seconds, 1),
            "sessions": int(payload.get("sessions", 0) or 0),
            "last_heartbeat_at": payload.get("last_heartbeat_at", ""),
        },
        "equity": {
            "current": None if current is None else str(current),
            "first": payload.get("first_equity", ""),
            "first_seen_at": payload.get("first_seen_at", ""),
            "at_arm": payload.get("equity_at_arm", ""),
            "armed_at": payload.get("armed_at", ""),
            "armed_source": payload.get("armed_source", ""),
        },
        "pnl": {
            "all_time": delta(first),
            "since_arming": delta(at_arm) if at_arm is not None else None,
        },
        "labels": {
            "all_time": (
                "current equity minus the first equity this agent ever saw"
            ),
            "since_arming": (
                "current equity minus the balance when submission was armed"
            ),
        },
    }
