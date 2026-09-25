"""Desk members: the benchmark strategy and the member wrapper."""

from __future__ import annotations

import unittest
from decimal import Decimal

from agentic_trading.desk.benchmark import BenchmarkStrategy
from agentic_trading.types import Side

D = Decimal


def quote(symbol: str, bid: str, ask: str, session: str = "regular") -> dict:
    return {
        "symbol": symbol,
        "bid": D(bid),
        "ask": D(ask),
        "observed_at": "2026-09-24T14:00:00Z",
        "market_session": session,
    }


class BenchmarkStrategyTests(unittest.TestCase):
    def test_buys_each_share_once_with_its_target_share(self) -> None:
        strategy = BenchmarkStrategy()
        [intent] = strategy.on_quote(quote("BTC-USD", "100", "101"))
        self.assertEqual((intent.symbol, intent.side), ("BTC-USD", Side.BUY))
        self.assertEqual(intent.metadata, {"target_share": "0.4"})
        self.assertEqual(intent.ref_price, D("101"))
        strategy.note_fill("BTC-USD", D("0.1"))
        self.assertEqual(strategy.on_quote(quote("BTC-USD", "100", "101")), [])

    def test_equities_wait_for_the_regular_session(self) -> None:
        strategy = BenchmarkStrategy()
        self.assertEqual(strategy.on_quote(quote("QQQ", "500", "500.1", "extended")), [])
        [intent] = strategy.on_quote(quote("QQQ", "500", "500.1"))
        self.assertEqual(intent.metadata, {"target_share": "0.6"})

    def test_other_symbols_and_seeded_holdings_are_ignored(self) -> None:
        strategy = BenchmarkStrategy()
        self.assertEqual(strategy.on_quote(quote("MSFT", "1", "1")), [])
        self.assertEqual(strategy.seed_positions({"QQQ": "0.05"}), 1)
        self.assertEqual(strategy.on_quote(quote("QQQ", "500", "500.1")), [])
        self.assertFalse(strategy.release_decision("crypto:2026-09-24"))


from agentic_trading.backtest import CostModel  # noqa: E402
from agentic_trading.desk.book import MemberBook  # noqa: E402
from agentic_trading.desk.member import Member  # noqa: E402
from agentic_trading.types import OrderIntent  # noqa: E402
from datetime import datetime, timezone  # noqa: E402

FREE = CostModel(D("0"), D("0"), D("0"))
NOW = datetime(2026, 9, 24, 14, tzinfo=timezone.utc)


class _Scripted:
    """Emits a fixed list of intents once, and records note_fill calls."""

    def __init__(self, intents: list[OrderIntent]) -> None:
        self.intents = intents
        self.fills: list[tuple[str, Decimal]] = []
        self.seeded: dict = {}

    def on_quote(self, quote: dict) -> list[OrderIntent]:
        out, self.intents = self.intents, []
        return out

    def note_fill(self, symbol: str, quantity: Decimal) -> None:
        self.fills.append((symbol, Decimal(str(quantity))))

    def seed_positions(self, positions: dict) -> int:
        self.seeded = dict(positions)
        return len(positions)


def _intent(symbol: str, side: Side, **kwargs) -> OrderIntent:
    return OrderIntent(
        decision_id=f"{symbol}-{side.value}",
        symbol=symbol,
        side=side,
        reason="test",
        created_at=NOW,
        quantity=kwargs.pop("quantity", D("1")),
        ref_price=kwargs.pop("ref_price", D("100")),
        **kwargs,
    )


def _member(strategy, *, order_pct: str = "0.19") -> Member:
    return Member(
        "m", strategy, MemberBook("m", starting_equity=D("50")), order_pct=D(order_pct)
    )


class MemberTests(unittest.TestCase):
    def test_an_entry_is_sized_by_order_pct_and_weight_at_the_live_ask(self) -> None:
        strategy = _Scripted([_intent("MSFT", Side.BUY, weight=D("0.5"))])
        member = _member(strategy)
        quotes = {"MSFT": quote("MSFT", "99", "100")}
        [event] = member.on_quote(quotes["MSFT"], quotes, FREE)
        self.assertEqual(event["event"], "member_fill")
        # 50 * 0.19 * 0.5 = 4.75 at the ask of 100
        self.assertEqual(member.book.positions["MSFT"], D("0.047500"))
        self.assertEqual(strategy.fills, [("MSFT", D("0.047500"))])

    def test_target_share_sizes_against_book_equity(self) -> None:
        strategy = _Scripted(
            [_intent("QQQ", Side.BUY, metadata={"target_share": "0.6"})]
        )
        member = _member(strategy)
        quotes = {"QQQ": quote("QQQ", "499", "500")}
        member.on_quote(quotes["QQQ"], quotes, FREE)
        self.assertEqual(member.book.positions["QQQ"], D("0.060000"))

    def test_an_exit_sells_at_the_live_bid_and_tells_the_strategy(self) -> None:
        strategy = _Scripted([_intent("MSFT", Side.BUY)])
        member = _member(strategy, order_pct="0.2")
        quotes = {"MSFT": quote("MSFT", "99", "100")}
        member.on_quote(quotes["MSFT"], quotes, FREE)
        held = member.book.positions["MSFT"]
        strategy.intents = [_intent("MSFT", Side.SELL, quantity=held)]
        member.on_quote(quotes["MSFT"], quotes, FREE)
        self.assertNotIn("MSFT", member.book.positions)
        self.assertEqual(strategy.fills[-1], ("MSFT", -held))
        self.assertAlmostEqual(float(member.book.cash), 50 - 10 + float(held) * 99, 6)

    def test_bar_keyed_symbols_are_filled_in_broker_form(self) -> None:
        # The trend strategy keys history as BTCUSD but emits BTC-USD intents;
        # a bare BTCUSD intent must still land on the BTC-USD position.
        strategy = _Scripted([_intent("BTCUSD", Side.BUY)])
        member = _member(strategy)
        quotes = {"BTC-USD": quote("BTC-USD", "99", "100")}
        member.on_quote(quotes["BTC-USD"], quotes, FREE)
        self.assertIn("BTC-USD", member.book.positions)
        self.assertNotIn("BTCUSD", member.book.positions)

    def test_a_raising_strategy_disables_only_this_member(self) -> None:
        class _Broken:
            def on_quote(self, quote: dict) -> list:
                raise RuntimeError("boom")

        member = _member(_Broken())
        q = quote("MSFT", "1", "1")
        [event] = member.on_quote(q, {"MSFT": q}, FREE)
        self.assertEqual(event["event"], "desk_member_failed")
        self.assertIn("boom", member.failed)
        self.assertEqual(member.on_quote(q, {"MSFT": q}, FREE), [])

    def test_the_book_is_authoritative_for_the_strategy_at_start(self) -> None:
        book = MemberBook("m", starting_equity=D("50"))
        book.buy("MSFT", D("10"), D("10"), FREE)
        strategy = _Scripted([])
        Member("m", strategy, book, order_pct=D("0.19"))
        self.assertEqual(strategy.seeded, {"MSFT": "1.000000"})
