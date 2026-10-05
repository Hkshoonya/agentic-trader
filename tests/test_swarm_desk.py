"""The desk reads the swarm's book like the switchboard's: judged and never written; the swarm is funded."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from agentic_trading.desk.allocator import UNFUNDED, hold_unfunded
from agentic_trading.desk.book import ReadOnlyBook
from agentic_trading.desk.member import ReadOnlyMember
from tests.test_desk_wiring import _config

MEMBERS = 'desk_members = ["momentum_rotation", "trend_crypto", "swarm", "benchmark"]'


class SwarmDeskTests(unittest.TestCase):
    def test_the_swarm_is_funded_and_keeps_its_weight(self) -> None:
        self.assertNotIn("swarm", UNFUNDED)
        held, would = hold_unfunded({"swarm": 0.5, "benchmark": 0.5}, benchmark="benchmark")
        self.assertEqual((held, would), ({"swarm": 0.5, "benchmark": 0.5}, {}))

    def test_the_swarm_is_a_read_only_member(self) -> None:
        from agentic_trading.cli import build_strategy

        with tempfile.TemporaryDirectory() as name:
            config = _config(Path(name), MEMBERS, "[swarm]", "enabled = true")
            desk = build_strategy(config)
        member = next(m for m in desk.members if m.name == "swarm")
        self.assertIsInstance(member, ReadOnlyMember)
        self.assertIsInstance(member.book, ReadOnlyBook)

    def test_the_swarm_needs_its_table_switched_on(self) -> None:
        for extra in ((MEMBERS,), (MEMBERS, "[swarm]", "enabled = false")):
            with self.subTest(extra=extra), tempfile.TemporaryDirectory() as name:
                with self.assertRaisesRegex(ValueError, r"\[swarm\] enabled"):
                    _config(Path(name), *extra)
