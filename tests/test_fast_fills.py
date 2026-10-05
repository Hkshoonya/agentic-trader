"""Paper fills use the real ask/bid after the delay; the mirror re-prices at Coinbase."""

from __future__ import annotations

import unittest
from datetime import timedelta
from decimal import Decimal

from agentic_trading.desk.book import MemberBook
from agentic_trading.fast.fills import Fill, Order, PaperFiller, usable_quote
from agentic_trading.fast.mirror import CoinbaseMirror
from tests.fast_support import T0, quote, trade

D = Decimal
MS = timedelta(milliseconds=1)


def _filler(cash="50"):
    book = MemberBook("switchboard", starting_equity=D(cash))
    return book, PaperFiller(book, D("0.0025"), delay=250 * MS)


class FillerTests(unittest.TestCase):
    def test_a_buy_fills_at_the_first_ask_after_the_delay(self) -> None:
        book, filler = _filler()
        filler.submit(Order("BTC/USD", "buy", T0, notional=D("10")))
        self.assertEqual(filler.on_tick(quote("BTC/USD", 99.9, 100, T0 + 100 * MS)), [])
        self.assertEqual(filler.on_tick(quote("ETH/USD", 9, 10, T0 + 300 * MS)), [])  # other coin
        [fill] = filler.on_tick(quote("BTC/USD", 100.9, 101, T0 + 300 * MS))
        self.assertEqual((fill.side, fill.price), ("buy", D("101")))
        self.assertAlmostEqual(float(fill.quantity), 10 / (101 * 1.0025), places=5)  # rounded down to 6 dp
        self.assertEqual(book.cash, D("50") - fill.quantity * D("101") * D("1.0025"))

    def test_a_sell_gets_the_bid_less_the_fee(self) -> None:
        book, filler = _filler()
        book.buy("BTC/USD", D("10"), D("100"), filler.costs)
        held, before = book.positions["BTC/USD"], book.cash
        filler.submit(Order("BTC/USD", "sell", T0, quantity=held))
        [fill] = filler.on_tick(quote("BTC/USD", 110, 110.2, T0 + 300 * MS))
        self.assertEqual((fill.price, fill.quantity), (D("110"), held))
        self.assertNotIn("BTC/USD", book.positions)
        self.assertEqual(book.cash, before + held * D("110") * D("0.9975"))

    def test_trades_and_crossed_quotes_never_fill(self) -> None:
        _, filler = _filler()
        filler.submit(Order("BTC/USD", "buy", T0, notional=D("10")))
        self.assertEqual(filler.on_tick(trade("BTC/USD", 100, T0 + 300 * MS)), [])
        self.assertEqual(filler.on_tick(quote("BTC/USD", 101, 100, T0 + 400 * MS)), [])
        self.assertFalse(usable_quote(quote("BTC/USD", 101, 100, T0)))
        self.assertTrue(usable_quote(quote("BTC/USD", 100, 100, T0)))
        self.assertEqual(len(filler.on_tick(quote("BTC/USD", 99.9, 100, T0 + 500 * MS))), 1)

    def test_an_order_under_the_minimum_reports_an_empty_fill(self) -> None:
        book, filler = _filler(cash="0.5")
        filler.submit(Order("BTC/USD", "buy", T0, notional=D("0.5")))
        [fill] = filler.on_tick(quote("BTC/USD", 99.9, 100, T0 + 300 * MS))
        self.assertEqual(fill.quantity, D("0"))
        self.assertEqual(book.positions, {})


class MirrorTests(unittest.TestCase):
    def _mirror(self):
        return CoinbaseMirror(MemberBook("switchboard@coinbase", starting_equity=D("50")), D("0.006"))

    def test_a_buy_and_a_sell_are_copied_at_coinbase_prices(self) -> None:
        mirror = self._mirror()
        self.assertEqual(CoinbaseMirror.product("BTC/USD"), "BTC-USD")
        mirror.on_tick(quote("BTC-USD", 100.0, 100.5, T0, venue="coinbase"))
        self.assertTrue(mirror.copy(Fill("BTC/USD", "buy", D("100"), D("0.1"), T0), T0 + timedelta(seconds=1)))
        held = mirror.book.positions["BTC/USD"]
        self.assertAlmostEqual(float(held), 10 / (100.5 * 1.006), places=5)
        mirror.on_tick(quote("BTC-USD", 110.0, 110.4, T0 + timedelta(seconds=9), venue="coinbase"))
        self.assertTrue(mirror.copy(Fill("BTC/USD", "sell", D("110"), D("0.1"), T0), T0 + timedelta(seconds=10)))
        self.assertEqual(mirror.book.positions, {})
        self.assertEqual(mirror.unpriced, 0)

    def test_a_missing_or_old_quote_is_unpriced(self) -> None:
        mirror = self._mirror()
        buy = Fill("BTC/USD", "buy", D("100"), D("0.1"), T0)
        self.assertFalse(mirror.copy(buy, T0))
        mirror.on_tick(quote("BTC-USD", 100.0, 100.5, T0, venue="coinbase"))
        self.assertFalse(mirror.copy(buy, T0 + timedelta(seconds=6)))
        self.assertEqual(mirror.unpriced, 2)
        # a sell with nothing copied to sell is not counted twice
        self.assertFalse(mirror.copy(Fill("BTC/USD", "sell", D("100"), D("0.1"), T0), T0))
        self.assertEqual(mirror.unpriced, 2)
