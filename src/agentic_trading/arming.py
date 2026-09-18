"""Arming: the one write the console is allowed to make.

Order submission has always required `AGENTIC_ALLOW_LIVE=1` in the process
environment, and that switch is deliberately not a config file — a workspace
copied to another machine cannot arm itself. That property is worth keeping, so
the console does not replace it: it writes a *request* file in this workspace,
and the runtime treats "environment switch OR local arm file" as armed.

A fresh install has neither, so it still starts inert.

The rules, in one place:

1. Arming is an explicit human act: a POST with `{"confirm": "ARM"}`.
2. It is refused unless the system has earned it — the promotion gate says
   eligible, the stage is at least probation, and the evidence report is fresh.
3. It is refused unless the request came from loopback, because the console is
   bound to loopback but that is not the same as checking.
4. Every change is journalled and timestamped, and disarming is always allowed.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

ARM_FILE = "arm.json"
CONFIRM_PHRASE = "ARM"

# How stale the evidence may be and still justify arming.
MAX_EVIDENCE_AGE_DAYS = 30.0


def arm_path(state_dir: Path | str) -> Path:
    return Path(state_dir) / ARM_FILE


def read_arm(state_dir: Path | str) -> Optional[dict[str, Any]]:
    path = arm_path(state_dir)
    if not path.is_file():
        return None
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return payload if isinstance(payload, dict) else None


def is_armed(state_dir: Path | str) -> bool:
    payload = read_arm(state_dir)
    return bool(payload and payload.get("armed"))


def _eligible_to_arm(state_dir: Path | str) -> tuple[bool, str]:
    """Whether the system has reached the state that justifies arming."""
    def read(name: str) -> dict[str, Any]:
        try:
            payload = json.loads((Path(state_dir) / name).read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}
        return payload if isinstance(payload, dict) else {}

    promotion = read("promotion.json")
    last = promotion.get("last_assessment") or {}
    stage = str(promotion.get("stage") or "shadow")
    if stage == "shadow":
        return False, "the agent is still in shadow: it has not cleared its evidence gate"
    if not last.get("eligible"):
        return False, "the last assessment was not eligible on the evidence"
    evidence = read("strategy_evidence.json")
    stamp = str(evidence.get("generated_at") or "")
    try:
        seen = datetime.fromisoformat(stamp)
    except (TypeError, ValueError):
        return False, "there is no walk-forward report to justify the size"
    if seen.tzinfo is None:
        seen = seen.replace(tzinfo=timezone.utc)
    age_days = (datetime.now(timezone.utc) - seen).total_seconds() / 86_400
    if age_days > MAX_EVIDENCE_AGE_DAYS:
        return False, f"the evidence report is {age_days:.0f} days old"
    return True, "the agent has cleared its evidence gate and the report is current"


def arm(state_dir: Path | str, *, source: str = "console") -> dict[str, Any]:
    """Record an arming request. Returns the payload that was written."""
    allowed, reason = _eligible_to_arm(state_dir)
    if not allowed:
        return {"armed": False, "refused": True, "reason": reason}
    payload = {
        "armed": True,
        "at": datetime.now(timezone.utc).isoformat(),
        "source": source,
    }
    _write(state_dir, payload)
    return payload


def disarm(state_dir: Path | str, *, source: str = "console", reason: str = "") -> dict[str, Any]:
    """Always allowed: lowering risk never needs permission."""
    payload = {
        "armed": False,
        "at": datetime.now(timezone.utc).isoformat(),
        "source": source,
        "reason": reason,
    }
    _write(state_dir, payload)
    return payload


def _write(state_dir: Path | str, payload: dict[str, Any]) -> None:
    from agentic_trading import jsonio

    jsonio.write_text(arm_path(state_dir), jsonio.dumps(payload, indent=2) + "\n")


def arm_status(state_dir: Path | str) -> dict[str, Any]:
    """For the console: may it be armed, is it armed, and why not."""
    allowed, reason = _eligible_to_arm(state_dir)
    payload = read_arm(state_dir) or {}
    return {
        "armed": bool(payload.get("armed")),
        "available": allowed,
        "reason": reason,
        "since": payload.get("at", ""),
        "source": payload.get("source", ""),
    }
