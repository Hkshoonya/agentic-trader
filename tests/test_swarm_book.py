"""One day of the swarm's book: mark at the close, then trade toward the blend."""

from __future__ import annotations

import unittest
from datetime import date, timedelta
from decimal import Decimal

from agentic_trading.backtest import CostModel
from agentic_trading.desk.book import MemberBook
from agentic_trading.swarm.book import advance, closes_on
from tests.swarm_support import daily

D = Decimal
SAT = date(2024, 1, 6)


def _book() -> MemberBook:
    return MemberBook("swarm", starting_equity=D("50"))


class BookTests(unittest.TestCase):
    def test_closes_are_keyed_the_desk_way_and_only_for_that_day(self) -> None:
        series = {"BTCUSD": daily("BTCUSD", [100, 101], start=date(2024, 1, 5)),
                  "SPY": daily("SPY", [400], start=date(2024, 1, 5))}
        self.assertEqual(closes_on(series, SAT), {"BTC-USD": D("101")})
        self.assertEqual(closes_on(series, date(2024, 1, 5)), {"BTC-USD": D("100"), "SPY": D("400")})

    def test_the_first_day_buys_the_targets_and_the_same_targets_trade_nothing(self) -> None:
        book = _book()
        prices = {"QQQ": D("400"), "BTC-USD": D("60000")}
        first = advance(book, date(2024, 1, 5), {"QQQ": 0.6, "BTCUSD": 0.4}, prices, CostModel())
        self.assertEqual(first, {"sold": 0, "bought": 2})
        self.assertAlmostEqual(book.weights()["QQQ"], 0.6, delta=0.01)
        again = advance(book, date(2024, 1, 6) + timedelta(days=2), {"QQQ": 0.6, "BTCUSD": 0.4}, prices, CostModel())
        self.assertEqual(again, {"sold": 0, "bought": 0})

    def test_a_dropped_target_is_sold_and_a_closed_market_waits(self) -> None:
        book = _book()
        advance(book, date(2024, 1, 5), {"QQQ": 0.5, "BTCUSD": 0.5}, {"QQQ": D("400"), "BTC-USD": D("60000")},
                CostModel())
        result = advance(book, SAT, {"BTCUSD": 1.0}, {"BTC-USD": D("60000")}, CostModel())
        self.assertIn("QQQ", book.positions)  # no QQQ close on a Saturday: it waits
        self.assertEqual(result["sold"], 0)
        result = advance(book, date(2024, 1, 8), {"BTCUSD": 1.0}, {"QQQ": D("401"), "BTC-USD": D("60000")},
                         CostModel())
        self.assertNotIn("QQQ", book.positions)
        self.assertEqual(result["sold"], 1)

    def test_each_day_in_order_leaves_one_sample(self) -> None:
        book = _book()
        for offset in range(5):
            advance(book, date(2024, 1, 1) + timedelta(days=offset), {"BTCUSD": 0.4},
                    {"BTC-USD": D(str(60000 + offset * 100))}, CostModel())
        self.assertEqual([d for d, _ in book.samples], ["2024-01-01", "2024-01-02", "2024-01-03", "2024-01-04"])
