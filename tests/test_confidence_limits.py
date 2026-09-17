"""The risk budget tracks evidence confidence, inside the operator's ceiling."""

from __future__ import annotations

import tempfile
import unittest
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace

from agentic_trading.limits import (
    GROW_FACTOR,
    MIN_ORDER_PCT,
    MIN_SCALE,
    propose_from_assessment,
)
from agentic_trading.promotion import (
    Assessment,
    PromotionPolicy,
    grade_confidence,
)


def config(tmp: Path, *, session_policy: str = "regular", ceiling: str = "") -> SimpleNamespace:
    return SimpleNamespace(
        max_order_pct=Decimal("0.05"),
        daily_notional_pct=Decimal("0.20"),
        session_policy=session_policy,
        max_session_policy=ceiling,
        state_dir=tmp,
    )


def assessment(confidence: float, **parts) -> Assessment:
    return Assessment(
        eligible=False,
        score=0.0,
        confidence=confidence,
        confidence_parts=parts,
    )


class ConfidenceGradingTests(unittest.TestCase):
    def test_strong_evidence_grades_higher_than_weak(self) -> None:
        policy = PromotionPolicy()
        strong, _ = grade_confidence(
            oos_trades=120,
            expectancy_bps=80.0,
            positive_fraction=1.0,
            max_drawdown_pct=1.0,
            bootstrap_p_value=0.0001,
            profit_factor=3.0,
            policy=policy,
        )
        weak, _ = grade_confidence(
            oos_trades=5,
            expectancy_bps=-20.0,
            positive_fraction=0.25,
            max_drawdown_pct=25.0,
            bootstrap_p_value=0.8,
            profit_factor=0.4,
            policy=policy,
        )
        self.assertGreater(strong, weak)
        # Weak evidence grades near zero — not exactly zero, because a small
        # sample that is merely unproven is not the same as evidence against.
        self.assertLess(weak, 0.15)
        self.assertLessEqual(strong, 1.0)

    def test_components_are_reported_for_audit(self) -> None:
        _, parts = grade_confidence(
            oos_trades=30,
            expectancy_bps=25.0,
            positive_fraction=0.75,
            max_drawdown_pct=7.5,
            bootstrap_p_value=0.025,
            profit_factor=1.5,
            policy=PromotionPolicy(),
        )
        self.assertEqual(
            set(parts),
            {"significance", "sample", "expectancy", "folds", "drawdown", "stability"},
        )
        self.assertAlmostEqual(parts["sample"], 1.0)
        self.assertAlmostEqual(parts["folds"], 0.75)
        self.assertAlmostEqual(parts["drawdown"], 0.5, places=3)


class ConfidenceLadderTests(unittest.TestCase):
    def test_zero_confidence_still_trades_at_the_floor(self) -> None:
        with tempfile.TemporaryDirectory() as name:
            limits = propose_from_assessment(
                config(Path(name)), assessment(0.0), current=None
            )
            self.assertEqual(
                Decimal(limits.max_order_pct),
                Decimal("0.05") * MIN_SCALE,
            )
            self.assertGreaterEqual(Decimal(limits.max_order_pct), MIN_ORDER_PCT)

    def test_full_confidence_reaches_but_never_exceeds_the_ceiling(self) -> None:
        with tempfile.TemporaryDirectory() as name:
            cfg = config(Path(name))
            current = propose_from_assessment(cfg, assessment(0.0), current=None)
            for _ in range(30):  # keep pulling the ladder as far as it will go
                current = propose_from_assessment(
                    cfg, assessment(1.0), current=current
                )
            self.assertEqual(Decimal(current.max_order_pct), Decimal("0.05"))
            self.assertEqual(Decimal(current.daily_notional_pct), Decimal("0.20"))

    def test_growth_is_rate_limited_to_one_step_per_assessment(self) -> None:
        with tempfile.TemporaryDirectory() as name:
            cfg = config(Path(name))
            start = propose_from_assessment(cfg, assessment(0.0), current=None)
            grown = propose_from_assessment(cfg, assessment(1.0), current=start)
            ceiling = Decimal("0.05")
            target = ceiling * MIN_SCALE  # first step is already at the floor
            self.assertEqual(Decimal(start.max_order_pct), target)
            self.assertLessEqual(
                Decimal(grown.max_order_pct),
                Decimal(start.max_order_pct) * GROW_FACTOR,
            )

    def test_flat_confidence_holds_the_budget(self) -> None:
        """No improvement, no growth: a repeated number must not ratchet risk."""
        with tempfile.TemporaryDirectory() as name:
            cfg = config(Path(name))
            first = propose_from_assessment(cfg, assessment(0.5), current=None)
            second = propose_from_assessment(cfg, assessment(0.5), current=first)
            self.assertEqual(first.max_order_pct, second.max_order_pct)
            self.assertEqual(second.reason, "confidence_flat")

    def test_falling_confidence_cuts_at_most_half(self) -> None:
        with tempfile.TemporaryDirectory() as name:
            cfg = config(Path(name))
            high = propose_from_assessment(cfg, assessment(1.0), current=None)
            cut = propose_from_assessment(cfg, assessment(0.0), current=high)
            self.assertEqual(cut.reason, "confidence_down")
            self.assertLessEqual(
                Decimal(cut.max_order_pct),
                Decimal(high.max_order_pct),
            )
            self.assertGreaterEqual(
                Decimal(cut.max_order_pct),
                Decimal(high.max_order_pct) * Decimal("0.5"),
            )

    def test_demotion_resets_to_the_floor(self) -> None:
        with tempfile.TemporaryDirectory() as name:
            cfg = config(Path(name))
            high = propose_from_assessment(cfg, assessment(1.0), current=None)
            reset = propose_from_assessment(
                cfg, assessment(1.0), current=high, reset=True
            )
            self.assertEqual(reset.reason, "reset_after_demotion")
            self.assertEqual(Decimal(reset.confidence), Decimal("0"))
            self.assertEqual(
                Decimal(reset.max_order_pct),
                Decimal("0.05") * MIN_SCALE,
            )

    def test_evidence_pass_still_lands_at_the_ceiling(self) -> None:
        with tempfile.TemporaryDirectory() as name:
            cfg = config(Path(name))
            current = propose_from_assessment(cfg, assessment(1.0), current=None)
            promoted = propose_from_assessment(cfg, assessment(1.0), current=current)
            self.assertLessEqual(
                Decimal(promoted.max_order_pct), Decimal("0.05")
            )
            self.assertEqual(promoted.details["ceiling_max_order_pct"], "0.05")


class SessionPolicyTests(unittest.TestCase):
    def test_hours_never_widen_without_an_operator_bound(self) -> None:
        with tempfile.TemporaryDirectory() as name:
            cfg = config(Path(name), session_policy="regular")  # no ceiling set
            limits = propose_from_assessment(cfg, assessment(1.0), current=None)
            self.assertEqual(limits.session_policy, "regular")

    def test_hours_widen_one_step_only_with_high_confidence(self) -> None:
        with tempfile.TemporaryDirectory() as name:
            cfg = config(Path(name), session_policy="regular", ceiling="any")
            timid = propose_from_assessment(cfg, assessment(0.4), current=None)
            self.assertEqual(timid.session_policy, "regular")

            bold = propose_from_assessment(cfg, assessment(0.9), current=timid)
            self.assertEqual(bold.session_policy, "extended")

            bolder = propose_from_assessment(cfg, assessment(0.99), current=bold)
            self.assertEqual(bolder.session_policy, "all")

    def test_hours_never_widen_past_the_operator_bound(self) -> None:
        with tempfile.TemporaryDirectory() as name:
            cfg = config(Path(name), session_policy="regular", ceiling="extended")
            current = None
            for _ in range(5):
                current = propose_from_assessment(
                    cfg, assessment(1.0), current=current
                )
            self.assertEqual(current.session_policy, "extended")


if __name__ == "__main__":
    unittest.main()
