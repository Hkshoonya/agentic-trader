"""Unit tests for SpyScalperStrategy streaming OrderIntents."""

from __future__ import annotations

import unittest
from datetime import datetime, timedelta
from decimal import Decimal

import paper_scalper
from agentic_trading.strategies.spy_scalper import SpyScalperStrategy
from agentic_trading.types import Side


def _quote(
    *,
    bid: str,
    ask: str,
    observed_at: str,
    quote_at: str | None = None,
    symbol: str = "SPY",
) -> dict:
    if quote_at is None:
        # Fresh by default: quote 1s before observation
        obs = datetime.fromisoformat(observed_at.replace("Z", "+00:00"))
        quote_at = (obs - timedelta(seconds=1)).strftime("%Y-%m-%dT%H:%M:%SZ")
    return {
        "symbol": symbol,
        "observed_at": observed_at,
        "quote_at": quote_at,
        "bid": bid,
        "ask": ask,
    }


class SpyScalperStrategyTests(unittest.TestCase):
    def setUp(self) -> None:
        # Tight spreads, short cooldown for readable tests
        self.config = paper_scalper.Config(
            symbol="SPY",
            initial_cash=Decimal("50"),
            max_spread_bps=Decimal("50"),  # allow wider test spreads
            slippage_bps=Decimal("1"),
            fee_per_order=Decimal("0"),
            take_profit_bps=Decimal("10"),
            stop_loss_bps=Decimal("10"),
            max_hold_seconds=60,
            cooldown_seconds=30,
            max_trades=3,
            daily_loss_limit=Decimal("1"),
            max_quote_age_seconds=10,
            max_signal_gap_seconds=30,
        )
        self.strategy = SpyScalperStrategy(self.config)

    def _three_rising_then_flat(self) -> list:
        """Three rising mids → BUY; return intents from the third quote."""
        q1 = _quote(bid="100.00", ask="100.02", observed_at="2026-09-15T12:00:00Z")
        q2 = _quote(bid="100.05", ask="100.07", observed_at="2026-09-15T12:00:05Z")
        q3 = _quote(bid="100.10", ask="100.12", observed_at="2026-09-15T12:00:10Z")
        self.assertEqual(self.strategy.on_quote(q1), [])
        self.assertEqual(self.strategy.on_quote(q2), [])
        return self.strategy.on_quote(q3)

    def test_three_rising_mids_emits_buy_with_quantity_and_ref_price(self) -> None:
        intents = self._three_rising_then_flat()
        self.assertEqual(len(intents), 1)
        buy = intents[0]
        self.assertEqual(buy.side, Side.BUY)
        self.assertEqual(buy.symbol, "SPY")
        self.assertEqual(buy.reason, "three_rising_midquotes")
        self.assertIsNotNone(buy.quantity)
        self.assertGreater(buy.quantity, Decimal("0"))
        self.assertEqual(buy.ref_price, Decimal("100.12"))  # ask
        # Sizing: spend up to cash at ask+slippage
        modeled_buy = Decimal("100.12") * (1 + Decimal("1") / Decimal("10000"))
        expected_qty = (Decimal("50") / modeled_buy).quantize(
            paper_scalper.QUANTITY_STEP, rounding=paper_scalper.ROUND_DOWN
        )
        self.assertEqual(buy.quantity, expected_qty)

    def test_take_profit_emits_sell(self) -> None:
        buy_intents = self._three_rising_then_flat()
        qty = buy_intents[0].quantity
        # Raise bid enough for ~10+ bps after modeled sell slippage
        # entry ~100.12 * 1.0001; need sell proceeds high enough
        q = _quote(
            bid="100.30",
            ask="100.32",
            observed_at="2026-09-15T12:00:20Z",
        )
        intents = self.strategy.on_quote(q)
        self.assertEqual(len(intents), 1)
        sell = intents[0]
        self.assertEqual(sell.side, Side.SELL)
        self.assertEqual(sell.reason, "take_profit")
        self.assertEqual(sell.quantity, qty)
        self.assertEqual(sell.ref_price, Decimal("100.30"))  # bid
        self.assertIsNotNone(sell.quantity)
        self.assertIsNotNone(sell.ref_price)

    def test_stop_loss_emits_sell(self) -> None:
        buy_intents = self._three_rising_then_flat()
        qty = buy_intents[0].quantity
        q = _quote(
            bid="99.90",
            ask="99.92",
            observed_at="2026-09-15T12:00:20Z",
        )
        intents = self.strategy.on_quote(q)
        self.assertEqual(len(intents), 1)
        sell = intents[0]
        self.assertEqual(sell.side, Side.SELL)
        self.assertEqual(sell.reason, "stop_loss")
        self.assertEqual(sell.quantity, qty)
        self.assertEqual(sell.ref_price, Decimal("99.90"))

    def test_timeout_emits_sell(self) -> None:
        buy_intents = self._three_rising_then_flat()
        qty = buy_intents[0].quantity
        # Hold prices flat-ish (no TP/SL) past max_hold_seconds
        q = _quote(
            bid="100.10",
            ask="100.12",
            observed_at="2026-09-15T12:01:15Z",  # 65s after entry at :10
        )
        intents = self.strategy.on_quote(q)
        self.assertEqual(len(intents), 1)
        sell = intents[0]
        self.assertEqual(sell.side, Side.SELL)
        self.assertEqual(sell.reason, "timeout")
        self.assertEqual(sell.quantity, qty)

    def test_wrong_symbol_no_intent(self) -> None:
        q = _quote(
            bid="100.00",
            ask="100.02",
            observed_at="2026-09-15T12:00:00Z",
            symbol="AAPL",
        )
        self.assertEqual(self.strategy.on_quote(q), [])

    def test_stale_quote_no_intent(self) -> None:
        q = _quote(
            bid="100.00",
            ask="100.02",
            observed_at="2026-09-15T12:00:20Z",
            quote_at="2026-09-15T12:00:00Z",  # 20s age > max 10
        )
        self.assertEqual(self.strategy.on_quote(q), [])

    def test_cooldown_blocks_immediate_reentry(self) -> None:
        self._three_rising_then_flat()
        # Force exit via stop
        self.strategy.on_quote(
            _quote(bid="99.90", ask="99.92", observed_at="2026-09-15T12:00:20Z")
        )
        # Immediately try three rising again within cooldown (30s)
        t0 = "2026-09-15T12:00:25Z"
        t1 = "2026-09-15T12:00:30Z"
        t2 = "2026-09-15T12:00:35Z"
        self.assertEqual(
            self.strategy.on_quote(_quote(bid="101.00", ask="101.02", observed_at=t0)),
            [],
        )
        self.assertEqual(
            self.strategy.on_quote(_quote(bid="101.05", ask="101.07", observed_at=t1)),
            [],
        )
        intents = self.strategy.on_quote(
            _quote(bid="101.10", ask="101.12", observed_at=t2)
        )
        self.assertEqual(intents, [])

    def test_reentry_after_cooldown(self) -> None:
        self._three_rising_then_flat()
        self.strategy.on_quote(
            _quote(bid="99.90", ask="99.92", observed_at="2026-09-15T12:00:20Z")
        )
        # After cooldown (>=30s from exit at :20)
        t0 = "2026-09-15T12:00:55Z"
        t1 = "2026-09-15T12:01:00Z"
        t2 = "2026-09-15T12:01:05Z"
        self.assertEqual(
            self.strategy.on_quote(_quote(bid="101.00", ask="101.02", observed_at=t0)),
            [],
        )
        self.assertEqual(
            self.strategy.on_quote(_quote(bid="101.05", ask="101.07", observed_at=t1)),
            [],
        )
        intents = self.strategy.on_quote(
            _quote(bid="101.10", ask="101.12", observed_at=t2)
        )
        self.assertEqual(len(intents), 1)
        self.assertEqual(intents[0].side, Side.BUY)
        self.assertIsNotNone(intents[0].quantity)
        self.assertIsNotNone(intents[0].ref_price)


if __name__ == "__main__":
    unittest.main()
