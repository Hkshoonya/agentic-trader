"""Adaptive risk limits: de-risk freely, never exceed the operator ceiling."""

from __future__ import annotations

import tempfile
import unittest
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace

from agentic_trading.limits import (
    MIN_ORDER_PCT,
    Limits,
    apply_to_guard,
    load_limits,
    propose,
    reconcile,
    save_limits,
)


def config(tmp: Path) -> SimpleNamespace:
    return SimpleNamespace(
        max_order_pct=Decimal("0.05"),
        daily_notional_pct=Decimal("0.20"),
        state_dir=tmp,
    )


class ReconcileTests(unittest.TestCase):
    """Lowering a ceiling must show up immediately, not at the next evaluation.

    Evaluations are skipped while the bars are unchanged — which is most days —
    so without this the state file (and every screen reading it) would keep
    advertising a budget the operator has already withdrawn.
    """

    def _stored(self, tmp: Path) -> Limits:
        stored = Limits(
            max_order_pct="0.03",
            daily_notional_pct="0.12",
            reason="confidence_target",
            updated_at="2026-01-01T00:00:00+00:00",
            confidence="0.5",
        )
        save_limits(tmp, stored)
        return stored

    def test_lowered_ceiling_clamps_the_budget(self) -> None:
        with tempfile.TemporaryDirectory() as name:
            tmp = Path(name)
            self._stored(tmp)
            cfg = SimpleNamespace(
                max_order_pct=Decimal("0.01"),
                daily_notional_pct=Decimal("0.04"),
                state_dir=tmp,
            )
            updated = reconcile(cfg)
            self.assertIsNotNone(updated)
            assert updated is not None
            self.assertEqual(Decimal(updated.max_order_pct), Decimal("0.01"))
            self.assertEqual(Decimal(updated.daily_notional_pct), Decimal("0.04"))
            self.assertEqual(updated.reason, "ceiling_lowered")
            self.assertEqual(
                updated.details.get("reconciled_from_max_order_pct"), "0.03"
            )
            stored = load_limits(tmp)
            assert stored is not None
            self.assertEqual(stored.max_order_pct, "0.01")

    def test_a_raised_ceiling_is_not_permission(self) -> None:
        """Only ever tightens: the agent may not grant itself the extra room."""
        with tempfile.TemporaryDirectory() as name:
            tmp = Path(name)
            self._stored(tmp)
            cfg = SimpleNamespace(
                max_order_pct=Decimal("0.50"),
                daily_notional_pct=Decimal("2.00"),
                state_dir=tmp,
            )
            updated = reconcile(cfg)
            assert updated is not None
            self.assertEqual(updated.max_order_pct, "0.03")
            self.assertEqual(updated.daily_notional_pct, "0.12")

    def test_reconciliation_settles_instead_of_churning(self) -> None:
        with tempfile.TemporaryDirectory() as name:
            tmp = Path(name)
            self._stored(tmp)
            cfg = SimpleNamespace(
                max_order_pct=Decimal("0.01"),
                daily_notional_pct=Decimal("0.04"),
                state_dir=tmp,
            )
            first = reconcile(cfg)
            second = reconcile(cfg)
            third = reconcile(cfg)
            assert first is not None and second is not None and third is not None
            self.assertEqual(first.updated_at, second.updated_at)
            self.assertEqual(second.to_dict(), third.to_dict())

    def test_no_stored_limits_is_not_an_error(self) -> None:
        with tempfile.TemporaryDirectory() as name:
            cfg = SimpleNamespace(
                max_order_pct=Decimal("0.01"),
                daily_notional_pct=Decimal("0.04"),
                state_dir=Path(name),
            )
            self.assertIsNone(reconcile(cfg))

    def test_floor_is_respected(self) -> None:
        """A ceiling below the floor still cannot disable the strategy entirely."""
        with tempfile.TemporaryDirectory() as name:
            tmp = Path(name)
            self._stored(tmp)
            cfg = SimpleNamespace(
                max_order_pct=Decimal("0.0001"),
                daily_notional_pct=Decimal("0.0001"),
                state_dir=tmp,
            )
            updated = reconcile(cfg)
            assert updated is not None
            self.assertEqual(Decimal(updated.max_order_pct), MIN_ORDER_PCT)


class ProposeTests(unittest.TestCase):
    def test_starting_limits_are_the_ceilings(self) -> None:
        with tempfile.TemporaryDirectory() as name:
            limits = propose(config(Path(name)), eligible=False, current=None)
            self.assertLess(Decimal(limits.max_order_pct), Decimal("0.05"))
            self.assertEqual(limits.reason, "evidence_not_passed_de_risk")

    def test_de_risking_happens_once_not_on_every_evaluation(self) -> None:
        """Repeated halving spirals below the minimum order and disables trading."""
        with tempfile.TemporaryDirectory() as name:
            cfg = config(Path(name))
            first = propose(cfg, eligible=False, current=None)
            again = propose(cfg, eligible=False, current=first)
            self.assertEqual(first.to_dict(), again.to_dict())
            self.assertEqual(Decimal(first.max_order_pct), Decimal("0.025"))
            self.assertGreaterEqual(Decimal(again.max_order_pct), MIN_ORDER_PCT)

    def test_passing_restores_the_ceiling_but_never_beyond(self) -> None:
        with tempfile.TemporaryDirectory() as name:
            cfg = config(Path(name))
            de_risked = propose(cfg, eligible=False, current=None)
            restored = propose(cfg, eligible=True, current=de_risked)
            self.assertEqual(Decimal(restored.max_order_pct), Decimal("0.05"))
            self.assertEqual(Decimal(restored.daily_notional_pct), Decimal("0.20"))
            self.assertEqual(restored.reason, "evidence_passed")

    def test_stored_limits_cannot_exceed_the_ceiling(self) -> None:
        with tempfile.TemporaryDirectory() as name:
            tmp = Path(name)
            cfg = config(tmp)
            # A tampered/corrupt file asking for 10x the ceiling.
            save_limits(
                tmp,
                Limits(
                    max_order_pct="0.50",
                    daily_notional_pct="2.00",
                    reason="tampered",
                    updated_at="",
                ),
            )
            guard = SimpleNamespace(
                max_order_pct=Decimal("0.05"),
                daily_notional_pct=Decimal("0.20"),
            )
            apply_to_guard(guard, cfg)
            self.assertEqual(guard.max_order_pct, Decimal("0.05"))
            self.assertEqual(guard.daily_notional_pct, Decimal("0.20"))

    def test_stored_limits_tighten_the_guard(self) -> None:
        with tempfile.TemporaryDirectory() as name:
            tmp = Path(name)
            cfg = config(tmp)
            save_limits(
                tmp,
                Limits(
                    max_order_pct="0.01",
                    daily_notional_pct="0.05",
                    reason="de_risked",
                    updated_at="",
                ),
            )
            guard = SimpleNamespace(
                max_order_pct=Decimal("0.05"),
                daily_notional_pct=Decimal("0.20"),
            )
            apply_to_guard(guard, cfg)
            self.assertEqual(guard.max_order_pct, Decimal("0.01"))
            self.assertEqual(guard.daily_notional_pct, Decimal("0.05"))

    def test_roundtrip_and_corrupt_file(self) -> None:
        with tempfile.TemporaryDirectory() as name:
            tmp = Path(name)
            self.assertIsNone(load_limits(tmp))
            save_limits(tmp, Limits("0.01", "0.05", "test", "now"))
            loaded = load_limits(tmp)
            self.assertEqual(loaded.max_order_pct, "0.01")
            (tmp / "effective_limits.json").write_text("{broken", encoding="utf-8")
            self.assertIsNone(load_limits(tmp))


if __name__ == "__main__":
    unittest.main()
