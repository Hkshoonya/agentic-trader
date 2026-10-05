"""The desk reads the switchboard's book, never writes it, and never funds it."""

from __future__ import annotations

import tempfile
import unittest
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from unittest.mock import patch

from agentic_trading.backtest import CostModel
from agentic_trading.desk.allocator import UNFUNDED, Allocation, hold_unfunded
from agentic_trading.desk.benchmark import BenchmarkStrategy
from agentic_trading.desk.book import MemberBook, ReadOnlyBook
from agentic_trading.desk.desk import StrategyDesk
from agentic_trading.desk.member import Member, ReadOnlyMember
from tests.test_desk import quote

D = Decimal
FREE = CostModel(D("0"), D("0"), D("0"))
NOW = datetime(2026, 9, 24, 14, 0, tzinfo=timezone.utc)


def _owner(path: Path) -> MemberBook:
    """The venues service's side: it owns and writes the switchboard's book."""
    book = MemberBook("switchboard", starting_equity=D("50"), path=path)
    book.samples = [("2026-09-22", "50.5")]
    book.entries = 2
    book.save()
    return book


class ReadOnlyBookTests(unittest.TestCase):
    def test_marks_and_saves_are_ignored_and_refresh_reads_the_owner(self) -> None:
        with tempfile.TemporaryDirectory() as name:
            path = Path(name) / "switchboard.json"
            owner = _owner(path)
            before = path.read_text()
            reader, broken = ReadOnlyBook.load(path, name="switchboard", starting_equity=D("50"))
            self.assertFalse(broken)
            self.assertFalse(reader.mark({"BTC/USD": D("100")}, NOW))
            reader.save()
            self.assertEqual(path.read_text(), before)
            owner.samples.append(("2026-09-23", "51"))
            owner.save()
            reader.refresh()
            self.assertEqual((len(reader.samples), reader.entries, reader.path), (2, 2, path))
            path.write_text("garbage")
            reader.refresh()  # a bad read keeps the last good copy: "nothing held" would sell it all
            self.assertEqual((len(reader.samples), reader.read_error), (2, "unreadable"))


class UnfundedTests(unittest.TestCase):
    def test_hold_unfunded_moves_the_weight_to_the_benchmark(self) -> None:
        self.assertIn("switchboard", UNFUNDED)
        held, would = hold_unfunded({"switchboard": 0.4, "momentum_rotation": 0.2, "benchmark": 0.4},
                                    benchmark="benchmark")
        self.assertEqual(held, {"switchboard": 0.0, "momentum_rotation": 0.2, "benchmark": 0.8})
        self.assertEqual(would, {"switchboard": 0.4})
        self.assertEqual(hold_unfunded({"benchmark": 1.0}, benchmark="benchmark"), ({"benchmark": 1.0}, {}))

    def test_the_desk_records_what_the_switchboard_would_earn_and_funds_nothing(self) -> None:
        with tempfile.TemporaryDirectory() as name:
            desk_dir = Path(name) / "desk"
            path = desk_dir / "switchboard.json"
            _owner(path)
            before = path.read_text()
            bench, _ = MemberBook.load(desk_dir / "benchmark.json", name="benchmark", starting_equity=D("50"))
            reader, _ = ReadOnlyBook.load(path, name="switchboard", starting_equity=D("50"))
            account, _ = MemberBook.load(desk_dir / "account.json", name="account", starting_equity=D("50"))
            log: list[dict] = []
            desk = StrategyDesk(
                members=[Member("benchmark", BenchmarkStrategy(), bench, order_pct=D("0.19")),
                         ReadOnlyMember("switchboard", reader)],
                account=account, costs=lambda: FREE, journal=log.append, state_path=desk_dir / "desk.json",
            )
            qualified = Allocation({"switchboard": 0.6, "benchmark": 0.4}, True, {"switchboard": "beats buy-and-hold"}, {})
            with patch("agentic_trading.desk.desk.allocate", return_value=qualified):
                desk.on_quote(quote("BTC-USD", "99", "100"))
            after = path.read_text()
        self.assertEqual(desk.allocations["switchboard"], 0.0)
        self.assertEqual(desk.allocations["benchmark"], 1.0)
        [event] = [e for e in log if e["event"] == "desk_allocation"]
        self.assertEqual(event["would_earn"], {"switchboard": 0.6})
        self.assertEqual(after, before)  # the desk saved its state but never this file


class WiringTests(unittest.TestCase):
    def test_switchboard_is_a_read_only_desk_member(self) -> None:
        from agentic_trading.cli import build_strategy
        from tests.test_desk_wiring import _config

        with tempfile.TemporaryDirectory() as name:
            config = _config(Path(name),
                             'desk_members = ["momentum_rotation", "trend_crypto", "switchboard", "benchmark"]')
            desk = build_strategy(config)
        member = next(m for m in desk.members if m.name == "switchboard")
        self.assertIsInstance(member, ReadOnlyMember)
        self.assertIsInstance(member.book, ReadOnlyBook)
