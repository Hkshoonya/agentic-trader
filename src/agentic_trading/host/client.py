"""Ask the Windows host what systemctl and journalctl answer on Linux, and ask it to act.

Reads ``state/host/status.json``; acts by dropping a request file and waiting for its ``.done``. A host
whose heartbeat is stale is not running, so nothing it last published counts as active.
"""

from __future__ import annotations

import json
import time
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Optional, Sequence

from agentic_trading import jsonio
from agentic_trading.host.supervisor import STAMP, host_dir

STALE = timedelta(seconds=30)


def status(state_dir: Path | str) -> dict[str, Any]:
    path = host_dir(state_dir) / "status.json"
    try:
        return dict(json.loads(jsonio.read_text(path))) if path.is_file() else {}
    except (OSError, ValueError, TypeError):
        return {}


def running(state: dict[str, Any], now: Optional[datetime] = None) -> bool:
    try:
        beat = datetime.fromisoformat(str(state.get("heartbeat")))
    except ValueError:
        return False
    return bool(state.get("up")) and (now or datetime.now(timezone.utc)) - beat < STALE


def unit_state(state_dir: Path | str, name: str, now: Optional[datetime] = None) -> str:
    """``systemctl is-active``: active, activating, inactive, failed, or unknown."""
    state = status(state_dir)
    bare = name.removesuffix(".service").removesuffix(".timer")
    row = (state.get("services") or {}).get(bare) or (state.get("timers") or {}).get(bare)
    if row is None:
        return "unknown"
    if not running(state, now):
        return "inactive"
    return str(row.get("state") or "unknown")


def result(state_dir: Path | str, name: str) -> str:
    """``systemctl show UNIT -p Result --value`` for a timer's oneshot."""
    bare = name.removesuffix(".service").removesuffix(".timer")
    return str(((status(state_dir).get("timers") or {}).get(bare) or {}).get("result") or "")


def logs_since(state_dir: Path | str, name: str, since: datetime) -> str:
    """``journalctl -u UNIT --since``: the unit's log lines stamped at or after ``since``."""
    bare = name.removesuffix(".service").removesuffix(".timer")
    from agentic_trading.host.units import find

    unit = find(bare)
    if unit is None:
        return ""
    cutoff = f"[{since.astimezone(timezone.utc):{STAMP}}]"
    path = Path(state_dir) / unit.log
    lines: list[str] = []
    for candidate in (path.with_name(path.name + ".1"), path):
        try:
            text = candidate.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        # A traceback's lines all carry the stamp of the moment they were written, so a cutoff on the
        # prefix keeps whole tracebacks together.
        lines += [line.split("] ", 1)[-1] for line in text.splitlines() if line[:len(cutoff)] >= cutoff]
    return "\n".join(lines)


def request(state_dir: Path | str, action: str, units: Sequence[str] = (), *, timeout: float = 180.0,
            poll: float = 0.25) -> tuple[bool, str]:
    """Hand the host one action and wait for its answer."""
    if not running(status(state_dir)):
        return False, "the host is not running (start it with: agentic-trading host run --config ...)"
    folder = host_dir(state_dir) / "requests"
    folder.mkdir(parents=True, exist_ok=True)
    name = f"{time.time_ns()}-{uuid.uuid4().hex[:8]}"
    done = folder / f"{name}.done"
    jsonio.write_text(folder / f"{name}.json", jsonio.dumps({"action": action, "units": list(units)}))
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if done.is_file():
            try:
                answer = json.loads(jsonio.read_text(done))
            except (OSError, ValueError):
                time.sleep(poll)
                continue
            done.unlink(missing_ok=True)
            return bool(answer.get("ok")), str(answer.get("note") or "")
        time.sleep(poll)
    (folder / f"{name}.json").unlink(missing_ok=True)
    return False, f"the host did not answer within {timeout:.0f}s"
