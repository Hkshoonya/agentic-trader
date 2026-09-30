"""The desk end to end: members, allocation, netting, throttling, restarts."""

from __future__ import annotations

import json
import tempfile
import unittest
from decimal import Decimal
from pathlib import Path

from agentic_trading.backtest import CostModel
from agentic_trading.desk.benchmark import BenchmarkStrategy
from agentic_trading.desk.book import MemberBook
from agentic_trading.desk.desk import StrategyDesk
from agentic_trading.desk.member import Member
from agentic_trading.types import Side

D = Decimal
FREE = CostModel(D("0"), D("0"), D("0"))
THU = "2026-09-24T14:00:00Z"
FRI = "2026-09-25T14:00:00Z"
MON = "2026-09-28T14:00:00Z"


def quote(symbol: str, bid: str, ask: str, at: str = THU, session: str = "regular") -> dict:
    return {
        "symbol": symbol,
        "bid": D(bid),
        "ask": D(ask),
        "observed_at": at,
        "market_session": session,
    }


class _Clock:
    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now


class _Broken:
    def on_quote(self, quote: dict) -> list:
        raise RuntimeError("boom")


def build(tmp: Path, *, extra: list[tuple[str, object]] = (), clock=None, events=None):
    desk_dir = tmp / "desk"
    members = []
    for name, strategy in [("benchmark", BenchmarkStrategy()), *extra]:
        book, _ = MemberBook.load(desk_dir / f"{name}.json", name=name, starting_equity=D("50"))
        members.append(Member(name, strategy, book, order_pct=D("0.19")))
    account, _ = MemberBook.load(desk_dir / "account.json", name="account", starting_equity=D("50"))
    log = events if events is not None else []
    desk = StrategyDesk(
        members=members,
        account=account,
        costs=lambda: FREE,
        journal=log.append,
        state_path=desk_dir / "desk.json",
        retry_seconds=900.0,
        monotonic=clock or _Clock(),
    )
    return desk, log


class DeskTests(unittest.TestCase):
    def test_the_benchmark_allocation_becomes_one_account_order(self) -> None:
        with tempfile.TemporaryDirectory() as name:
            desk, log = build(Path(name))
            [intent] = desk.on_quote(quote("BTC-USD", "99", "100"))
        self.assertEqual((intent.symbol, intent.side), ("BTC-USD", Side.BUY))
        # The benchmark member put 40% of $50 into BTC; the account follows 100%.
        # 40% of the member book, copied at 100%: ~0.2 BTC (0.2004 after marks).
        self.assertAlmostEqual(float(intent.quantity), 0.2, delta=0.002)
        kinds = [event["event"] for event in log]
        self.assertIn("member_fill", kinds)
        self.assertIn("desk_allocation", kinds)
        self.assertEqual(desk.allocations["benchmark"], 1.0)

    def test_a_filled_gap_and_later_price_drift_emit_nothing(self) -> None:
        with tempfile.TemporaryDirectory() as name:
            desk, _ = build(Path(name))
            [intent] = desk.on_quote(quote("BTC-USD", "99", "100"))
            desk.note_fill(intent.symbol, intent.quantity)
            self.assertEqual(desk.on_quote(quote("BTC-USD", "99", "100")), [])
            self.assertEqual(desk.on_quote(quote("BTC-USD", "149", "150")), [])

    def test_an_unfilled_gap_is_retried_only_after_the_retry_window(self) -> None:
        with tempfile.TemporaryDirectory() as name:
            clock = _Clock()
            desk, _ = build(Path(name), clock=clock)
            self.assertEqual(len(desk.on_quote(quote("BTC-USD", "99", "100"))), 1)
            clock.now = 60.0
            self.assertEqual(desk.on_quote(quote("BTC-USD", "99", "100")), [])
            clock.now = 901.0
            self.assertEqual(len(desk.on_quote(quote("BTC-USD", "99", "100"))), 1)

    def test_a_partial_fill_lets_the_rest_go_at_once(self) -> None:
        with tempfile.TemporaryDirectory() as name:
            desk, _ = build(Path(name))
            [first] = desk.on_quote(quote("BTC-USD", "99", "100"))
            desk.note_fill("BTC-USD", first.quantity / 2)
            [rest] = desk.on_quote(quote("BTC-USD", "99", "100"))
        self.assertAlmostEqual(float(rest.quantity), float(first.quantity) / 2, places=5)

    def test_equities_wait_for_the_regular_session(self) -> None:
        with tempfile.TemporaryDirectory() as name:
            desk, _ = build(Path(name))
            self.assertEqual(desk.on_quote(quote("QQQ", "500", "500", session="extended")), [])
            [intent] = desk.on_quote(quote("QQQ", "500", "500"))
        self.assertEqual(intent.symbol, "QQQ")

    def test_holdings_seeded_before_any_price_are_sold_only_once_priced(self) -> None:
        with tempfile.TemporaryDirectory() as name:
            desk, _ = build(Path(name))
            self.assertEqual(desk.seed_positions({"SOL-USD": "0.05"}), 1)
            # No SOL price yet: account value unknown, nothing may be emitted.
            self.assertEqual(desk.on_quote(quote("BTC-USD", "99", "100")), [])
            [sell] = desk.on_quote(quote("SOL-USD", "200", "201"))
        self.assertEqual((sell.side, sell.quantity), (Side.SELL, D("0.05")))

    def test_allocation_runs_once_a_week_and_survives_a_restart(self) -> None:
        with tempfile.TemporaryDirectory() as name:
            tmp = Path(name)
            events: list[dict] = []
            desk, _ = build(tmp, events=events)
            desk.on_quote(quote("BTC-USD", "99", "100", at=THU))
            restarted, _ = build(tmp, events=events)
            self.assertEqual(restarted.allocation_week, "2026-09-21")
            restarted.on_quote(quote("BTC-USD", "99", "100", at=FRI))
            restarted.on_quote(quote("BTC-USD", "99", "100", at=MON))
        weeks = [e["week"] for e in events if e["event"] == "desk_allocation"]
        self.assertEqual(weeks, ["2026-09-21", "2026-09-28"])

    def test_a_failing_member_loses_its_capital_and_the_desk_continues(self) -> None:
        with tempfile.TemporaryDirectory() as name:
            tmp = Path(name)
            (tmp / "desk").mkdir()
            (tmp / "desk" / "desk.json").write_text(
                json.dumps(
                    {
                        "allocations": {"benchmark": 0.4, "broken": 0.6},
                        "allocation_week": "2026-09-21",
                        "targets": {},
                    }
                ),
                encoding="utf-8",
            )
            desk, log = build(tmp, extra=[("broken", _Broken())])
            intents = desk.on_quote(quote("BTC-USD", "99", "100"))
        self.assertEqual(desk.allocations, {"benchmark": 1.0, "broken": 0.0})
        self.assertIn("desk_member_failed", [e["event"] for e in log])
        self.assertEqual(len(intents), 1)  # the benchmark still trades


COSTS = CostModel(D("0"), D("0"), D("0"), crypto=CostModel(D("0"), D("0"), D("0.05")))


class DeskHardeningTests(unittest.TestCase):
    def test_price_moves_do_not_retarget_while_a_symbol_waits_for_a_quote(self) -> None:
        with tempfile.TemporaryDirectory() as name:
            tmp = Path(name)
            desk, _ = build(tmp)
            [qqq] = desk.on_quote(quote("QQQ", "500", "500"))
            desk.note_fill("QQQ", qqq.quantity)
            [btc] = desk.on_quote(quote("BTC-USD", "99", "100"))
            desk.note_fill("BTC-USD", btc.quantity)
            # Restart over the weekend: the benchmark holds QQQ, but no QQQ
            # quote arrives until Monday. BTC keeps moving.
            events: list[dict] = []
            restarted, _ = build(tmp, events=events)
            restarted.seed_positions(dict(desk.account.positions))
            for price in ("110", "120", "130", "140"):
                restarted.on_quote(quote("BTC-USD", price, price, at=FRI))
        retargets = [e for e in events if e["event"] == "desk_targets"]
        self.assertLessEqual(len(retargets), 1)

    def test_a_save_failure_is_journaled_not_raised(self) -> None:
        with tempfile.TemporaryDirectory() as name:
            tmp = Path(name)
            desk, log = build(tmp)
            blocker = tmp / "blocked"
            blocker.write_text("x", encoding="utf-8")
            desk.state_path = blocker / "desk.json"  # parent is a file
            desk.on_quote(quote("BTC-USD", "99", "100"))
        self.assertIn("desk_save_failed", [e["event"] for e in log])

    def test_the_daily_sample_is_taken_before_the_new_days_trades(self) -> None:
        with tempfile.TemporaryDirectory() as name:
            tmp = Path(name)
            desk_dir = tmp / "desk"
            book, _ = MemberBook.load(desk_dir / "benchmark.json", name="benchmark", starting_equity=D("50"))
            account, _ = MemberBook.load(desk_dir / "account.json", name="account", starting_equity=D("50"))
            desk = StrategyDesk(
                members=[Member("benchmark", BenchmarkStrategy(), book, order_pct=D("0.19"))],
                account=account,
                costs=lambda: COSTS,
                journal=[].append,
                state_path=desk_dir / "desk.json",
                monotonic=_Clock(),
            )
            desk.on_quote(quote("MSFT", "10", "10", at=THU))  # day 1: no trade
            desk.on_quote(quote("BTC-USD", "99", "100", at=FRI))  # day 2 buys BTC
        # Day 1's closing equity is untouched by day 2's $0.05 crypto fee.
        self.assertEqual(
            [(day, D(value)) for day, value in book.samples], [("2026-09-24", D("50"))]
        )
