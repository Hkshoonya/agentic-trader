from decimal import Decimal
from datetime import datetime, timezone
import unittest

from agentic_trading.types import OrderIntent, Side
from agentic_trading.config import load_config


class OrderIntentTests(unittest.TestCase):
    def test_notional_from_quantity_and_ref_price(self):
        intent = OrderIntent(
            decision_id="d1",
            symbol="spy",
            side=Side.BUY,
            quantity=Decimal("2"),
            ref_price=Decimal("100"),
            reason="test",
            created_at=datetime(2026, 9, 15, tzinfo=timezone.utc),
        )
        self.assertEqual(intent.symbol, "SPY")
        self.assertEqual(intent.resolved_notional(), Decimal("200"))

    def test_rejects_missing_notional_inputs(self):
        with self.assertRaises(ValueError):
            OrderIntent(
                decision_id="d1",
                symbol="SPY",
                side=Side.BUY,
                reason="x",
                created_at=datetime(2026, 9, 15, tzinfo=timezone.utc),
            )


class ConfigTests(unittest.TestCase):
    def test_load_example_defaults_shadow(self):
        cfg = load_config("config/agentic.example.toml")
        self.assertEqual(cfg.mode, "shadow")
        self.assertEqual(cfg.max_order_pct, Decimal("0.05"))
        self.assertEqual(cfg.symbol_whitelist, frozenset({"SPY"}))


if __name__ == "__main__":
    unittest.main()
