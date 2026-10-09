"""The self-upgrader as the console sees it: named fields only."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from agentic_trading.upgrade.control import load_control

LAST_FIELDS = ("at", "outcome", "message", "task")


def _pick(raw: Any, names: tuple[str, ...]) -> dict[str, Any]:
    return {name: raw.get(name) for name in names} if isinstance(raw, dict) and raw else {}


def upgrade_view(state_dir: Path | str) -> dict[str, Any]:
    control = load_control(state_dir)
    try:
        status = json.loads((Path(state_dir) / "upgrade.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        status = {}
    if not isinstance(status, dict):
        status = {}
    last = status.get("last") if isinstance(status.get("last"), dict) else {}
    return {"enabled": bool(status.get("enabled")), "paused": control.paused, "reason": control.reason,
            "rollback_requested": control.rollback_requested,
            "canary": _pick(control.canary, ("title", "until", "pr")),
            "last_shipped": _pick(control.last_shipped, ("title", "pr")),
            "last": {name: last.get(name) for name in LAST_FIELDS}}
