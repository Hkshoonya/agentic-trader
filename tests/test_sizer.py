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


class ProportionalSizingTests(unittest.TestCase):
    """Size in proportion to the strategy's weighting, not flat dollars.

    The strategy computes an inverse-volatility weight — 1.50 for a 9%-vol ETF,
    0.33 for a 60%-vol pair — and the runtime used to ignore it, giving every
    symbol the same dollars. On the current universe that flat book carried
    23.9% max drawdown at 2.04% per order; the proportional book carries 9.0%
    for the same trades and the same bps per trade.
    """

    def _intent(self, weight):
        from decimal import Decimal as D

        from agentic_trading.types import OrderIntent, Side

        return OrderIntent(
            decision_id="w",
            symbol="SPY",
            side=Side.BUY,
            quantity=D("1"),
            ref_price=D("100"),
            reason="trend_entry",
            created_at=datetime(2026, 9, 18, tzinfo=timezone.utc),
            weight=weight,
        )

    def test_flat_is_still_flat(self) -> None:
        sized = size_intent(
            self._intent(Decimal("0.33")),
            equity=Decimal("100"),
            max_order_pct=Decimal("0.05"),
            proportional=False,
        )
        assert sized is not None
        self.assertEqual(sized.resolved_notional(), Decimal("5.00"))

    def test_a_wild_symbol_gets_a_smaller_share(self) -> None:
        quiet = size_intent(
            self._intent(Decimal("1.5")),
            equity=Decimal("100"),
            max_order_pct=Decimal("0.05"),
            proportional=True,
        )
        wild = size_intent(
            self._intent(Decimal("0.33")),
            equity=Decimal("100"),
            max_order_pct=Decimal("0.05"),
            proportional=True,
        )
        assert quiet is not None and wild is not None
        # 1.5 is capped at 1.0: quiet assets do not exceed the operator's ceiling.
        self.assertEqual(quiet.resolved_notional(), Decimal("5.00"))
        self.assertAlmostEqual(float(wild.resolved_notional()), 1.65, places=2)
        self.assertLess(wild.resolved_notional(), quiet.resolved_notional())

    def test_the_proportions_match_the_weights(self) -> None:
        base = size_intent(
            self._intent(Decimal("1.0")),
            equity=Decimal("100"),
            max_order_pct=Decimal("0.05"),
            proportional=True,
        )
        third = size_intent(
            self._intent(Decimal("0.33")),
            equity=Decimal("100"),
            max_order_pct=Decimal("0.05"),
            proportional=True,
        )
        assert base is not None and third is not None
        ratio = float(third.resolved_notional() / base.resolved_notional())
        self.assertAlmostEqual(ratio, 0.33, places=2)

    def test_no_weight_falls_back_to_flat(self) -> None:
        """Strategies that predate weights keep their old sizing exactly."""
        sized = size_intent(
            self._intent(None),
            equity=Decimal("100"),
            max_order_pct=Decimal("0.05"),
            proportional=True,
        )
        assert sized is not None
        self.assertEqual(sized.resolved_notional(), Decimal("5.00"))

    def test_a_weight_too_small_to_place_is_placed_at_the_minimum(self) -> None:
        """A scaled order under the minimum is no order, so the minimum wins.

        This reversed an earlier decision. Refusing was defensible in isolation
        — $50 × 2.04% × 0.33 really is $0.34 — but on a small account every
        weight is below 1, so the rule refused *the whole book*: at 00:00 UTC on
        2026-09-18 the live daemon's only daily rebalance was thrown away this
        way, with the per-order cap already raised for the express purpose of
        clearing Robinhood's $1.00 minimum.

        The minimum is not a licence to trade bigger: the order lands on $1.02,
        never above the unweighted ceiling, and a ceiling that cannot clear the
        minimum still refuses (the next test).
        """
        sized = size_intent(
            self._intent(Decimal("0.33")),
            equity=Decimal("50"),
            max_order_pct=Decimal("0.0204"),
            proportional=True,
        )
        self.assertIsNotNone(sized, "the book would never trade at this weight")
        assert sized is not None
        notional = sized.resolved_notional()
        self.assertGreaterEqual(notional, Decimal("1.00"))
        self.assertLessEqual(notional, Decimal("1.02"))

    def test_the_minimum_never_exceeds_the_unweighted_ceiling(self) -> None:
        """The floor is bounded by the operator's ceiling, not by the minimum."""
        sized = size_intent(
            self._intent(Decimal("0.33")),
            equity=Decimal("50"),
            max_order_pct=Decimal("0.009"),  # $0.45 ceiling
            proportional=True,
        )
        self.assertIsNone(sized)

    def test_an_unreadable_weight_does_not_break_sizing(self) -> None:
        from decimal import Decimal as D

        from agentic_trading.types import OrderIntent, Side

        intent = OrderIntent(
            decision_id="bad",
            symbol="SPY",
            side=Side.BUY,
            quantity=D("1"),
            ref_price=D("100"),
            reason="trend_entry",
            created_at=datetime(2026, 9, 18, tzinfo=timezone.utc),
            weight="not-a-number",  # type: ignore[arg-type]
        )
        sized = size_intent(
            intent,
            equity=Decimal("100"),
            max_order_pct=Decimal("0.05"),
            proportional=True,
        )
        assert sized is not None
        self.assertEqual(sized.resolved_notional(), Decimal("5.00"))
