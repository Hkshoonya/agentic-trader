"""``state/upgrade/control.json``: the one-click switches and the open canary.

Pause always wins. A file that can't be read reads as paused: the upgrader must never take a broken
switch for permission.
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from agentic_trading import jsonio


@dataclass
class Control:
    paused: bool = False
    reason: str = ""
    rollback_requested: bool = False
    canary: dict[str, Any] = field(default_factory=dict)
    last_shipped: dict[str, Any] = field(default_factory=dict)


def _path(state_dir: Path | str) -> Path:
    return Path(state_dir) / "upgrade" / "control.json"


def load_control(state_dir: Path | str) -> Control:
    path = _path(state_dir)
    if not path.is_file():
        return Control()
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
        return Control(bool(raw.get("paused")), str(raw.get("reason") or ""),
                       bool(raw.get("rollback_requested")), dict(raw.get("canary") or {}),
                       dict(raw.get("last_shipped") or {}))
    except (OSError, ValueError, TypeError, AttributeError):
        return Control(paused=True, reason="control.json is unreadable: paused until it is fixed")


def save_control(state_dir: Path | str, control: Control) -> None:
    path = _path(state_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    jsonio.write_text(path, jsonio.dumps(asdict(control), indent=2) + "\n")


def pause(state_dir: Path | str, reason: str) -> Control:
    control = load_control(state_dir)
    control.paused, control.reason = True, reason
    save_control(state_dir, control)
    return control


def resume(state_dir: Path | str) -> Control:
    control = load_control(state_dir)
    control.paused, control.reason = False, ""
    save_control(state_dir, control)
    return control


def request_rollback(state_dir: Path | str) -> Control:
    control = load_control(state_dir)
    control.rollback_requested = True
    save_control(state_dir, control)
    return control
