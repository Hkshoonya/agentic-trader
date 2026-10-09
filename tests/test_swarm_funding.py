"""A funded swarm: the desk follows its book daily, never sells on a bad read, and drops a stale book."""

from __future__ import annotations

import json
import tempfile
import unittest
from decimal import Decimal
from pathlib import Path
from unittest.mock import patch

from agentic_trading.backtest import CostModel
from agentic_trading.desk.allocator import UNFUNDED, Allocation, hold_unfunded
from agentic_trading.desk.benchmark import BenchmarkStrategy
from agentic_trading.desk.book import MemberBook, ReadOnlyBook
from agentic_trading.desk.desk import BOOK_POLL_SECONDS, StrategyDesk
from agentic_trading.desk.member import Member, ReadOnlyMember
from tests.test_desk import THU, _Clock, quote

D = Decimal
FREE = CostModel(D("0"), D("0"), D("0"))


def _write_swarm(
    path: Path,
    positions: dict[str, str],
    prices: dict[str, str],
    mark_day: str = "2026-09-23",
) -> None:
    """The swarm job's side: it owns and writes the swarm's book."""
    book = MemberBook("swarm", starting_equity=D("50"), path=path)
    book.positions = {s: D(q) for s, q in positions.items()}
    book.prices = {s: D(p) for s, p in prices.items()}
    book.cash = D("50") - sum(
        (book.positions[s] * book.prices[s] for s in book.positions), D("0")
    )
    book.samples = [("2026-09-22", "50")]
    book.entries = 1
    book._mark_day = mark_day
    book.save()


class _Rig:
    def __init__(
        self, folder: Path, weight: float, mark_day: str = "2026-09-23"
    ) -> None:
        desk_dir = folder / "desk"
        self.path = desk_dir / "swarm.json"
        _write_swarm(self.path, {"BTC-USD": "0.2"}, {"BTC-USD": "100"}, mark_day)
        bench, _ = MemberBook.load(
            desk_dir / "benchmark.json", name="benchmark", starting_equity=D("50")
        )
        self.reader, _ = ReadOnlyBook.load(
            self.path, name="swarm", starting_equity=D("50")
        )
        account, _ = MemberBook.load(
            desk_dir / "account.json", name="account", starting_equity=D("50")
        )
        self.log: list[dict] = []
        self.clock = _Clock()
        self.desk = StrategyDesk(
            members=[
                Member("benchmark", BenchmarkStrategy(), bench, order_pct=D("0.19")),
                ReadOnlyMember("swarm", self.reader),
            ],
            account=account,
            costs=lambda: FREE,
            journal=self.log.append,
            state_path=desk_dir / "desk.json",
            monotonic=self.clock,
        )
        chosen = Allocation(
            {"swarm": weight, "benchmark": 1 - weight},
            True,
            {"swarm": "beats buy-and-hold"},
            {},
        )
        with patch("agentic_trading.desk.desk.allocate", return_value=chosen):
            self.desk.on_quote(quote("BTC-USD", "99", "101", THU))

    def targets(self) -> list[dict]:
        return [e for e in self.log if e["event"] == "desk_targets"]

    def later_quote(self, symbol: str, bid: str, ask: str) -> None:
        self.clock.now += BOOK_POLL_SECONDS + 1
        self.desk.on_quote(quote(symbol, bid, ask, THU))


class FundingTests(unittest.TestCase):
    def test_the_swarm_is_funded_and_the_switchboard_is_not(self) -> None:
        self.assertEqual(UNFUNDED, frozenset({"switchboard"}))
        held, would = hold_unfunded(
            {"swarm": 0.5, "benchmark": 0.5}, benchmark="benchmark"
        )
        self.assertEqual((held, would), ({"swarm": 0.5, "benchmark": 0.5}, {}))

    def test_a_funded_swarm_book_reaches_the_account_targets(self) -> None:
        with tempfile.TemporaryDirectory() as name:
            rig = _Rig(Path(name), 0.5)
        self.assertEqual(rig.desk.allocations["swarm"], 0.5)
        btc = D(rig.targets()[-1]["targets"]["BTC-USD"])
        # weight × equity × book share / price, for the swarm and for the benchmark's own 40% BTC leg
        self.assertAlmostEqual(
            float(btc), 0.5 * 50 * 0.4 / 100 + 0.5 * 50 * 0.4 / 100, delta=0.01
        )

    def test_a_mid_week_change_to_the_swarm_book_is_followed(self) -> None:
        with tempfile.TemporaryDirectory() as name:
            rig = _Rig(Path(name), 0.5)
            before = len(rig.targets())
            _write_swarm(rig.path, {"ETH-USD": "1"}, {"ETH-USD": "20"})
            rig.later_quote("ETH-USD", "19.9", "20.1")
        self.assertGreater(len(rig.targets()), before)
        eth = D(rig.targets()[-1]["targets"]["ETH-USD"])
        self.assertAlmostEqual(float(eth), 0.5 * 50 * 0.4 / 20, delta=0.01)

    def test_the_book_is_not_reread_before_the_poll_interval(self) -> None:
        with tempfile.TemporaryDirectory() as name:
            rig = _Rig(Path(name), 0.5)
            before = len(rig.targets())
            _write_swarm(rig.path, {"ETH-USD": "1"}, {"ETH-USD": "20"})
            rig.desk.on_quote(
                quote("ETH-USD", "19.9", "20.1", THU)
            )  # same instant: no poll yet
        self.assertEqual(len(rig.targets()), before)

    def test_an_unfunded_change_moves_no_targets(self) -> None:
        with tempfile.TemporaryDirectory() as name:
            rig = _Rig(Path(name), 0.0)
            before = len(rig.targets())
            _write_swarm(rig.path, {"ETH-USD": "1"}, {"ETH-USD": "20"})
            rig.later_quote("ETH-USD", "19.9", "20.1")
        self.assertEqual(len(rig.targets()), before)

    def test_an_unreadable_book_keeps_the_last_good_copy(self) -> None:
        with tempfile.TemporaryDirectory() as name:
            rig = _Rig(Path(name), 0.5)
            before = len(rig.targets())
            rig.path.write_text("{garbage")
            rig.later_quote("BTC-USD", "99", "101")
            rig.later_quote("BTC-USD", "99", "101")
        self.assertEqual(len(rig.targets()), before)  # nothing sold
        self.assertEqual(rig.reader.positions, {"BTC-USD": D("0.2")})
        self.assertEqual(
            len([e for e in rig.log if e["event"] == "desk_member_unreadable"]), 1
        )

    def test_a_stale_book_hands_its_weight_back_only_while_stale(self) -> None:
        with tempfile.TemporaryDirectory() as name:
            rig = _Rig(Path(name), 0.5, mark_day="2026-09-15")  # nine days before Thursday's quote
            stale_btc = rig.targets()[-1]["targets"].get("BTC-USD")
            self.assertEqual(rig.desk.allocations["swarm"], 0.5)  # the allocator's decision is kept
            _write_swarm(rig.path, {"BTC-USD": "0.2"}, {"BTC-USD": "100"}, mark_day="2026-09-23")
            rig.later_quote("BTC-USD", "99", "101")
            fresh_btc = rig.targets()[-1]["targets"]["BTC-USD"]
        self.assertAlmostEqual(float(D(stale_btc)), 0.0 + 1.0 * 50 * 0.4 / 100, delta=0.01)  # benchmark only
        self.assertAlmostEqual(float(D(fresh_btc)), 0.2, delta=0.01)  # the swarm's share is back
        self.assertEqual(len([e for e in rig.log if e["event"] == "desk_member_stale"]), 1)

    def test_a_restart_with_a_missing_book_freezes_targets_and_keeps_the_weight(self) -> None:
        with tempfile.TemporaryDirectory() as name:
            folder = Path(name)
            rig = _Rig(folder, 0.5)
            before = rig.targets()[-1]["targets"]
            rig.path.unlink()
            reader, _ = ReadOnlyBook.load(rig.path, name="swarm", starting_equity=D("50"))
            bench, _ = MemberBook.load(folder / "desk" / "benchmark.json", name="benchmark", starting_equity=D("50"))
            account, _ = MemberBook.load(folder / "desk" / "account.json", name="account", starting_equity=D("50"))
            log: list[dict] = []
            clock = _Clock()
            desk = StrategyDesk(
                members=[Member("benchmark", BenchmarkStrategy(), bench, order_pct=D("0.19")),
                         ReadOnlyMember("swarm", reader)],
                account=account, costs=lambda: FREE, journal=log.append,
                state_path=folder / "desk" / "desk.json", monotonic=clock)
            desk.on_quote(quote("BTC-USD", "99", "101", THU))
            self.assertEqual(desk.allocations["swarm"], 0.5)
            self.assertEqual(desk.targets, {s: D(q) for s, q in before.items()})
            self.assertEqual([e for e in log if e["event"] == "desk_targets"], [])
            _write_swarm(rig.path, {"BTC-USD": "0.2"}, {"BTC-USD": "100"})
            clock.now += BOOK_POLL_SECONDS + 1
            desk.on_quote(quote("BTC-USD", "99", "101", THU))
        self.assertEqual(desk.allocations["swarm"], 0.5)

    def test_an_unfunded_change_never_resizes_the_account_after_prices_move(self) -> None:
        with tempfile.TemporaryDirectory() as name:
            rig = _Rig(Path(name), 0.0)
            rig.desk.on_quote(quote("BTC-USD", "109", "111", THU))  # a 10% move: no event retargets
            before = len(rig.targets())
            _write_swarm(rig.path, {"ETH-USD": "1"}, {"ETH-USD": "20"})
            rig.later_quote("ETH-USD", "19.9", "20.1")
        self.assertEqual(len(rig.targets()), before)

    def test_a_funded_change_resizes_only_that_members_symbols(self) -> None:
        with tempfile.TemporaryDirectory() as name:
            rig = _Rig(Path(name), 0.5)
            rig.desk.on_quote(quote("QQQ", "399", "401", THU))
            qqq_before = rig.targets()[-1]["targets"].get("QQQ")
            rig.desk.on_quote(quote("QQQ", "439", "441", THU))  # QQQ +10%: no retarget by itself
            _write_swarm(rig.path, {"ETH-USD": "1"}, {"ETH-USD": "20"})
            rig.later_quote("ETH-USD", "19.9", "20.1")
            last = rig.targets()[-1]["targets"]
        self.assertIn("ETH-USD", last)
        self.assertEqual(last.get("QQQ"), qqq_before)  # untouched: not the swarm's symbol


    def test_a_follow_records_the_gap_from_the_swarms_paper_price(self) -> None:
        with tempfile.TemporaryDirectory() as name:
            rig = _Rig(Path(name), 0.5)
            _write_swarm(rig.path, {"ETH-USD": "1"}, {"ETH-USD": "20"})
            rig.later_quote("ETH-USD", "20.1", "20.3")  # mid 20.2: 1% above the swarm's close
            gaps = list(rig.desk.follow)
            saved = json.loads((Path(name) / "desk" / "desk.json").read_text())["follow"]
        self.assertEqual(gaps, [100.0])
        self.assertEqual(saved, [100.0])


class RefreshTests(unittest.TestCase):
    def test_refresh_if_changed_reads_only_a_changed_file(self) -> None:
        with tempfile.TemporaryDirectory() as name:
            path = Path(name) / "swarm.json"
            _write_swarm(path, {"BTC-USD": "0.2"}, {"BTC-USD": "100"})
            reader, _ = ReadOnlyBook.load(path, name="swarm", starting_equity=D("50"))
            self.assertFalse(reader.refresh_if_changed())
            _write_swarm(path, {"ETH-USD": "1"}, {"ETH-USD": "20"})
            self.assertTrue(reader.refresh_if_changed())
            self.assertEqual(set(reader.positions), {"ETH-USD"})
            self.assertFalse(reader.refresh_if_changed())
            data = json.loads(path.read_text())
            data["samples"].append(
                ["2026-09-23", "50.1"]
            )  # a new sample, same holdings
            path.write_text(json.dumps(data))
            self.assertFalse(
                reader.refresh_if_changed()
            )  # holdings unchanged: no retarget needed
            self.assertEqual(len(reader.samples), 2)
