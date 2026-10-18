"""The services and timers, as ``deploy/`` declares them for systemd.

These mirror the unit files field for field (``tests/test_host_units.py`` reads both and fails on any
drift), so a Windows host restarts, schedules and alerts exactly as the Linux one does.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, time, timedelta, timezone
from typing import Optional

SANDBOX_MISSING = ("off on this host: its sandbox needs Linux; build its container to turn it on "
                   "(agentic-trading host container build --config ...)")


def sandbox_available() -> bool:
    import os
    import shutil

    return os.name != "nt" and shutil.which("bwrap") is not None

CONFIG = "config/agentic.toml"


@dataclass(frozen=True)
class Service:
    """A long-running unit (``Type=simple``)."""

    name: str
    args: tuple[str, ...]
    log: str  # file name under state_dir, as StandardOutput=append: names it
    restart: str  # "always" or "on-failure"
    restart_seconds: float
    env: dict[str, str] = field(default_factory=dict)
    prevent_exit: tuple[int, ...] = ()  # RestartPreventExitStatus
    stop_seconds: float = 90.0  # TimeoutStopSec (systemd's default is 90 s)
    start_limit: Optional[tuple[int, float]] = None  # (StartLimitBurst, StartLimitIntervalSec)
    alert_on_failure: bool = False  # OnFailure=agentic-trading-alert.service


@dataclass(frozen=True)
class Timer:
    """A oneshot unit and the timer that starts it."""

    name: str
    args: tuple[str, ...]
    log: str
    timeout_seconds: float  # TimeoutStartSec
    daily_utc: Optional[time] = None  # OnCalendar=*-*-* HH:MM:SS UTC
    every_seconds: Optional[float] = None  # OnUnitActiveSec
    boot_seconds: Optional[float] = None  # OnBootSec
    persistent: bool = False  # Persistent=true: a run missed while the host was down happens at start
    low_priority: bool = False  # Nice=10
    env_file: Optional[str] = None  # sourced by the launcher, relative to the home directory
    notify_result: bool = False  # the launcher tells the operator how the run went
    # Runs candidate code: only where its sandbox exists (bwrap, Linux). Elsewhere the host shows why it
    # is off rather than waking it nightly to refuse.
    needs_sandbox: bool = False

    def next_run(self, last: Optional[datetime], booted: datetime, now: datetime) -> datetime:
        """When to fire next, worked out at start and again after each run, as systemd does.

        ``last`` is the previous start as recorded on disk (None: never). A result at or before ``now``
        means "fire now": a persistent timer catches up on the one run it missed while the host was down.
        """
        if self.daily_utc is not None:
            today = datetime.combine(now.astimezone(timezone.utc).date(), self.daily_utc, timezone.utc)
            latest = today if today <= now else today - timedelta(days=1)
            if self.persistent and last is not None and last < latest:
                return latest
            return latest + timedelta(days=1)
        if last is None or last < booted:
            return booted + timedelta(seconds=self.boot_seconds or 0.0)
        return last + timedelta(seconds=self.every_seconds or 0.0)


SERVICES: tuple[Service, ...] = (
    # AGENTIC_ALLOW_AUTONOMY=1 as the systemd unit sets it; AGENTIC_ALLOW_LIVE stays unset (a manual override).
    Service("agentic-trading", ("run", "--config", CONFIG, "--daemon"), "daemon.log", "always", 15.0,
            env={"AGENTIC_ALLOW_AUTONOMY": "1"}, start_limit=(15, 900.0), alert_on_failure=True),
    Service("agentic-trading-dashboard", ("dashboard", "--config", CONFIG, "--port", "8787"), "dashboard.log",
            "always", 5.0),
    Service("agentic-trading-venues", ("venues", "run", "--config", CONFIG), "venues.log", "on-failure", 10.0,
            prevent_exit=(2,), stop_seconds=20.0),
)

TIMERS: tuple[Timer, ...] = (
    Timer("agentic-trading-swarm", ("swarm", "step", "--config", CONFIG), "swarm.log", 45 * 60.0,
          daily_utc=time(0, 30), persistent=True, low_priority=True),
    Timer("agentic-trading-upgrade", ("upgrade", "run", "--config", CONFIG), "upgrade.log", 330 * 60.0,
          daily_utc=time(2, 0), persistent=True, low_priority=True,
          env_file=".config/agentic-trading/upgrade.env", notify_result=True, needs_sandbox=True),
    Timer("agentic-trading-upgrade-watch", ("upgrade", "watch", "--config", CONFIG), "upgrade.log", 10 * 60.0,
          every_seconds=300.0, boot_seconds=300.0),
)

NAMES = tuple(unit.name for unit in (*SERVICES, *TIMERS))


def find(name: str) -> Service | Timer | None:
    """A unit by its systemd name, with or without the ``.service`` suffix."""
    bare = name.removesuffix(".service").removesuffix(".timer")
    return next((unit for unit in (*SERVICES, *TIMERS) if unit.name == bare), None)
