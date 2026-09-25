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
