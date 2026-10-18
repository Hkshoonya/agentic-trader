"""The service manager's commands: systemd on Linux, the ``agentic-trading host`` supervisor on Windows.

Both go through the upgrader's runner, so tests see (and fake) every command either host would run.
The Windows verbs answer as systemctl does: ``is-active`` prints the state, ``show -p Result --value``
prints a oneshot's result, ``logs --since`` prints the unit's lines since a UTC stamp.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path
from typing import Optional, Sequence

from agentic_trading.host.units import CONFIG


def windows() -> bool:
    return os.name == "nt"


def _host(*verb: str, config: Optional[Path] = None) -> list[str]:
    return [sys.executable, "-m", "agentic_trading", "host", verb[0], "--config", str(config or CONFIG), *verb[1:]]


def restart(services: Sequence[str], *, config: Optional[Path] = None) -> list[str]:
    if windows():
        return _host("restart", *services, config=config)
    return ["systemctl", "--user", "restart", *services]


def is_active(service: str) -> list[str]:
    return _host("is-active", service) if windows() else ["systemctl", "--user", "is-active", service]


def logs_since(service: str, stamp: str) -> list[str]:
    if windows():
        return _host("logs", service, "--since", stamp)
    return ["journalctl", "--user", "-u", service, "--since", stamp, "--no-pager", "-o", "cat"]


def result(unit: str) -> list[str]:
    if windows():
        return _host("show", unit, "-p", "Result", "--value")
    return ["systemctl", "--user", "show", unit, "-p", "Result", "--value"]
