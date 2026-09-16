"""Equity-aware sizing: entries fit the cap, exits are never resized."""

from __future__ import annotations

import unittest
from datetime import datetime, timezone
from decimal import Decimal

from agentic_trading.sizer import size_intent
from agentic_trading.types import OrderIntent, Side


def intent(
    *,
    side: Side = Side.BUY,
    quantity: str = "0.01",
    ref_price: str = "660",
    decision_id: str = "d1",
) -> OrderIntent:
    return OrderIntent(
        decision_id=decision_id,
        symbol="SPY",
        side=side,
        quantity=Decimal(quantity),
        ref_price=Decimal(ref_price),
        reason="test",
        created_at=datetime(2026, 9, 16, tzinfo=timezone.utc),
    )


class SizerTests(unittest.TestCase):
    def test_entry_within_cap_is_unchanged(self) -> None:
        original = intent(quantity="0.003", ref_price="660")  # $1.98
        sized = size_intent(
            original,
            equity=Decimal("50"),
            max_order_pct=Decimal("0.05"),
            min_notional=Decimal("1.00"),
        )
        self.assertEqual(sized.quantity, original.quantity)

    def test_entry_over_cap_is_resized_down(self) -> None:
        # 0.01 SPY at $660 = $6.60, but 5% of $50 is $2.50.
        sized = size_intent(
            intent(quantity="0.01", ref_price="660"),
            equity=Decimal("50"),
            max_order_pct=Decimal("0.05"),
            min_notional=Decimal("1.00"),
        )
        self.assertIsNotNone(sized)
        self.assertLessEqual(sized.resolved_notional(), Decimal("2.50"))
        self.assertEqual(sized.quantity, Decimal("0.003787"))
        self.assertEqual(sized.decision_id, "d1")

    def test_exit_is_never_resized(self) -> None:
        original = intent(side=Side.SELL, quantity="0.01", ref_price="660")
        sized = size_intent(
            original,
            equity=Decimal("50"),
            max_order_pct=Decimal("0.05"),
            min_notional=Decimal("1.00"),
        )
        self.assertEqual(sized.quantity, original.quantity)

    def test_account_too_small_returns_none(self) -> None:
        # 5% of $10 is $0.50, below the $1.00 minimum order.
        sized = size_intent(
            intent(quantity="0.01", ref_price="660"),
            equity=Decimal("10"),
            max_order_pct=Decimal("0.05"),
            min_notional=Decimal("1.00"),
        )
        self.assertIsNone(sized)

    def test_zero_equity_returns_none(self) -> None:
        self.assertIsNone(
            size_intent(
                intent(),
                equity=Decimal("0"),
                max_order_pct=Decimal("0.05"),
            )
        )

    def test_tiny_valid_entry_below_floor_is_refused(self) -> None:
        self.assertIsNone(
            size_intent(
                intent(quantity="0.001", ref_price="660"),  # $0.66
                equity=Decimal("50"),
                max_order_pct=Decimal("0.05"),
                min_notional=Decimal("1.00"),
            )
        )


if __name__ == "__main__":
    unittest.main()
