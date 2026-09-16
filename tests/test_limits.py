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
    save_limits,
)


def config(tmp: Path) -> SimpleNamespace:
    return SimpleNamespace(
        max_order_pct=Decimal("0.05"),
        daily_notional_pct=Decimal("0.20"),
        state_dir=tmp,
    )


class ProposeTests(unittest.TestCase):
    def test_starting_limits_are_the_ceilings(self) -> None:
        with tempfile.TemporaryDirectory() as name:
            limits = propose(config(Path(name)), eligible=False, current=None)
            self.assertLess(Decimal(limits.max_order_pct), Decimal("0.05"))
            self.assertEqual(limits.reason, "evidence_not_passed_de_risk")

    def test_repeated_failure_keeps_de_risking_then_floors(self) -> None:
        with tempfile.TemporaryDirectory() as name:
            cfg = config(Path(name))
            current = None
            for _ in range(12):
                current = propose(cfg, eligible=False, current=current)
            self.assertGreaterEqual(Decimal(current.max_order_pct), MIN_ORDER_PCT)
            self.assertLess(Decimal(current.max_order_pct), Decimal("0.01"))

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
