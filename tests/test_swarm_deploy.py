"""The swarm's timer runs daily, catches up after downtime, and runs one step at a time."""

from __future__ import annotations

import unittest
from pathlib import Path

DEPLOY = Path(__file__).resolve().parent.parent / "deploy"


class DeployTests(unittest.TestCase):
    def test_the_timer_is_daily_and_persistent(self) -> None:
        timer = (DEPLOY / "agentic-trading-swarm.timer").read_text()
        self.assertIn("OnCalendar=*-*-* 00:30:00 UTC", timer)
        self.assertIn("Persistent=true", timer)

    def test_the_service_is_a_oneshot_step(self) -> None:
        service = (DEPLOY / "agentic-trading-swarm.service").read_text()
        self.assertIn("Type=oneshot", service)
        self.assertIn("ExecStart=/home/doczeus/.local/bin/agentic-trading-swarm", service)
        launcher = (DEPLOY / "agentic-trading-swarm").read_text()
        self.assertIn("swarm step --config config/agentic.toml", launcher)
