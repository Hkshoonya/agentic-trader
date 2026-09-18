"""Equity-aware sizing: entries fit the cap, exits are never resized."""

from __future__ import annotations

import unittest
from datetime import datetime, timezone
from decimal import Decimal

from agentic_trading.sizer import floor_cap, size_intent
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


class SmallAccountFloorTests(unittest.TestCase):
    """Small-account mode: raise the cap just enough to place one order.

    The alternative is refusing every entry, which is what the agent did on a
    $50 account: a 0.92% cap is $0.46 and the broker minimum is $1.00.
    """

    def test_it_does_nothing_when_disabled(self) -> None:
        cap = floor_cap(
            equity=Decimal("50"),
            policy_cap=Decimal("0.0092"),
            min_notional=Decimal("1"),
            max_cap=Decimal("0"),
        )
        self.assertEqual(cap, Decimal("0.0092"))

    def test_it_does_nothing_when_the_account_is_big_enough(self) -> None:
        cap = floor_cap(
            equity=Decimal("500"),
            policy_cap=Decimal("0.0092"),
            min_notional=Decimal("1"),
            max_cap=Decimal("0.02"),
        )
        self.assertEqual(cap, Decimal("0.0092"))

    def test_it_raises_the_cap_to_clear_the_minimum(self) -> None:
        cap = floor_cap(
            equity=Decimal("50"),
            policy_cap=Decimal("0.0092"),
            min_notional=Decimal("1"),
            max_cap=Decimal("0.02"),
        )
        self.assertEqual(cap, Decimal("0.02"))
        # The whole point: the order is now placeable.
        self.assertGreaterEqual(Decimal("50") * cap, Decimal("1"))

    def test_the_raised_cap_carries_a_margin_for_rounding(self) -> None:
        """A $1.00 target must not become a $0.9997 order after rounding."""
        cap = floor_cap(
            equity=Decimal("1000"),
            policy_cap=Decimal("0.0005"),
            min_notional=Decimal("1"),
            max_cap=Decimal("0.02"),
        )
        self.assertGreater(Decimal("1000") * cap, Decimal("1"))

    def test_it_never_exceeds_the_operator_ceiling(self) -> None:
        """$5 of equity cannot be fixed by a bigger fraction, and must not try."""
        cap = floor_cap(
            equity=Decimal("5"),
            policy_cap=Decimal("0.0092"),
            min_notional=Decimal("1"),
            max_cap=Decimal("0.02"),
        )
        self.assertEqual(cap, Decimal("0.02"), "capped, not raised to 20%")

    def test_it_fades_out_as_the_account_grows(self) -> None:
        caps = [
            floor_cap(
                equity=Decimal(str(equity)),
                policy_cap=Decimal("0.0092"),
                min_notional=Decimal("1"),
                max_cap=Decimal("0.02"),
            )
            for equity in (50, 100, 109, 120, 500)
        ]
        # Monotonic non-increasing: the floor releases its grip, never grabs more.
        self.assertEqual(caps, sorted(caps, reverse=True))
        self.assertEqual(caps[-1], Decimal("0.0092"))

    def test_a_floor_sized_intent_actually_survives_the_sizer(self) -> None:
        """End to end: the same intent that was refused is now placeable."""
        from datetime import datetime, timezone
        from decimal import Decimal as D

        from agentic_trading.sizer import size_intent
        from agentic_trading.types import OrderIntent, Side

        intent = OrderIntent(
            decision_id="d",
            symbol="BCH-USD",
            side=Side.BUY,
            quantity=D("1"),
            ref_price=D("233.85"),
            reason="trend_entry",
            created_at=datetime(2026, 9, 18, tzinfo=timezone.utc),
        )
        # At the evidence cap the account is too small…
        self.assertIsNone(
            size_intent(
                intent, equity=D("50"), max_order_pct=D("0.0092"), min_notional=D("1")
            )
        )
        # …a ceiling of exactly the need is *not* enough either: $1.00 of
        # notional rounds down to six decimals and lands a hair under the
        # minimum. That is why floor_cap carries a margin and why the runtime
        # reports an insufficient authorised ceiling instead of silently
        # refusing.
        self.assertIsNone(
            size_intent(
                intent, equity=D("50"), max_order_pct=D("0.02"), min_notional=D("1")
            )
        )
        # With the margin-aware cap (1.02/50 = 2.04%, inside a 3% authorisation)
        # the same intent is placeable.
        cap = floor_cap(
            equity=D("50"),
            policy_cap=D("0.0092"),
            min_notional=D("1"),
            max_cap=D("0.03"),
        )
        self.assertEqual(cap, D("0.0204"))
        sized = size_intent(
            intent, equity=D("50"), max_order_pct=cap, min_notional=D("1")
        )
        self.assertIsNotNone(sized)
        assert sized is not None
        self.assertGreaterEqual(sized.resolved_notional(), D("1"))
