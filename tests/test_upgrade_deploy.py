"""The upgrader runs once a day and catches up; its watchdog runs every five minutes."""

from __future__ import annotations

import unittest
from pathlib import Path

DEPLOY = Path(__file__).resolve().parent.parent / "deploy"


class UpgradeDeployTests(unittest.TestCase):
    def test_the_upgrade_timer_is_daily_and_persistent(self) -> None:
        timer = (DEPLOY / "agentic-trading-upgrade.timer").read_text()
        self.assertIn("OnCalendar=*-*-* 02:00:00 UTC", timer)
        self.assertIn("Persistent=true", timer)

    def test_the_watchdog_timer_runs_every_five_minutes(self) -> None:
        timer = (DEPLOY / "agentic-trading-upgrade-watch.timer").read_text()
        self.assertIn("OnBootSec=5min", timer)
        self.assertIn("OnUnitActiveSec=5min", timer)

    def test_the_launchers_run_and_watch(self) -> None:
        for unit, command in (("agentic-trading-upgrade", "upgrade run --config config/agentic.toml"),
                              ("agentic-trading-upgrade-watch", "upgrade watch --config config/agentic.toml")):
            with self.subTest(unit=unit):
                service = (DEPLOY / f"{unit}.service").read_text()
                self.assertIn("Type=oneshot", service)
                self.assertIn(f"ExecStart=/home/doczeus/.local/bin/{unit}", service)
                self.assertIn(command, (DEPLOY / unit).read_text())

    def test_systemd_never_kills_a_run_before_its_own_timeouts_do(self) -> None:
        # write 45 + review 20 + two test counts 20 + suite 40 + doc counts 15 + git, PR and restart ~18
        # = ~158 min. A unit killed mid-deploy would leave live moved with no canary to watch it.
        import re

        service = (DEPLOY / "agentic-trading-upgrade.service").read_text()
        minutes = int(re.search(r"TimeoutStartSec=(\d+)min", service).group(1))
        self.assertGreaterEqual(minutes, 180)
