"""The operator's daily budget follows account size, and confidence scales it.

The schedule the operator asked for, verbatim: under $200 the day's budget is
70% of the account at no confidence and 90% once the evidence has earned it;
above $500 it is 50%, above $1000 25%, above $2000 20%. A $50 account spread
over four positions is $12 a position — at the flat 1% ceiling it would take
fifty days to become fully invested, which is not a trading system.

What these tests protect is the *shape* of that: the tiers, the confidence
scaling between them, the derived per-order ceiling, that an old config with no
schedule keeps its flat ceilings, and that a budget cut for a reason is never
silently re-priced back up.
"""

from __future__ import annotations

import json
import tempfile
import unittest
from decimal import Decimal
from pathlib import Path
from typing import Any

from agentic_trading.config import Config, load_config
from agentic_trading.limits import (
    Limits,
    budget_ceilings,
    reconcile,
    save_limits,
    scheduled_daily_bounds,
    scheduled_daily_share,
)

SCHEDULE = (
    (200.0, Decimal("0.90"), Decimal("0.70")),
    (500.0, Decimal("0.50"), Decimal("0.35")),
    (1000.0, Decimal("0.25"), Decimal("0.175")),
    (2000.0, Decimal("0.20"), Decimal("0.14")),
)


def _config(tmp: Path, **overrides: Any) -> Config:
    values: dict[str, str] = {
        "mode": '"shadow"',
        "strategy": '"fixture"',
        "symbol_whitelist": '["SPY"]',
        "max_order_pct": '"0.01"',
        "daily_notional_pct": '"0.04"',
        "daily_loss_pct": '"0.03"',
        "max_open_positions": "4",
        "equity_refresh_ticks": "30",
        "equity_refresh_seconds": "60",
        "timezone": '"local"',
        "quotes_path": f'"{tmp / "quotes.jsonl"}"',
        "journal_dir": f'"{tmp / "journal"}"',
        "state_dir": f'"{tmp / "state"}"',
        "tools_snapshot_path": f'"{tmp / "tools.json"}"',
        "token_path": f'"{tmp / "missing.json"}"',
        "mcp_url": '"https://agent.robinhood.com/mcp/trading"',
    }
    values.update({key: str(value) for key, value in overrides.items()})
    path = tmp / "agentic.toml"
    path.write_text(
        "\n".join(f"{key} = {value}" for key, value in values.items()) + "\n",
        encoding="utf-8",
    )
    return load_config(path)


class ScheduleTierTests(unittest.TestCase):
    def _cfg(self, tmp: Path) -> Config:
        base = _config(tmp)
        return Config(**{**base.__dict__, "daily_budget_schedule": SCHEDULE})

    def test_the_tiers_are_the_ones_the_operator_asked_for(self) -> None:
        with tempfile.TemporaryDirectory() as name:
            cfg = self._cfg(Path(name))
            cases = {
                50: (0.70, 0.90),
                200: (0.70, 0.90),
                500: (0.35, 0.50),
                1000: (0.175, 0.25),
                2000: (0.14, 0.20),
                25000: (0.14, 0.20),
            }
            for equity, (low, high) in cases.items():
                # The helper returns (at full confidence, at zero confidence).
                ceiling, floor = scheduled_daily_bounds(cfg, equity)
                self.assertAlmostEqual(float(floor), low, places=4, msg=f"${equity}")
                self.assertAlmostEqual(float(ceiling), high, places=4, msg=f"${equity}")

    def test_confidence_moves_the_budget_across_its_band(self) -> None:
        with tempfile.TemporaryDirectory() as name:
            cfg = self._cfg(Path(name))
            # The operator's words: "70% or 90%" for an account under $200.
            self.assertAlmostEqual(
                float(scheduled_daily_share(cfg, 50, 0.0)), 0.70, places=4
            )
            self.assertAlmostEqual(
                float(scheduled_daily_share(cfg, 50, 1.0)), 0.90, places=4
            )
            middle = float(scheduled_daily_share(cfg, 50, 0.5))
            self.assertAlmostEqual(middle, 0.80, places=4)

    def test_the_band_moves_between_rows_instead_of_stepping(self) -> None:
        with tempfile.TemporaryDirectory() as name:
            cfg = self._cfg(Path(name))
            shares = [
                float(scheduled_daily_share(cfg, equity, 0.5))
                for equity in range(100, 1001, 25)
            ]
            self.assertEqual(shares, sorted(shares, reverse=True))
            # No single step may more than double the risk it replaces: the
            # whole point of interpolating is that growth is not a cliff.
            for before, after in zip(shares, shares[1:]):
                if after > 0:
                    self.assertLess(before / after, 2.0)

    def test_the_per_order_ceiling_is_a_share_of_the_day(self) -> None:
        with tempfile.TemporaryDirectory() as name:
            cfg = self._cfg(Path(name))
            order, daily = budget_ceilings(cfg, equity=50, confidence=1.0)
            self.assertAlmostEqual(float(daily), 0.90, places=4)
            # Four open slots: one order may take a quarter of the day and no
            # more, so a single fill cannot spend the whole budget.
            self.assertAlmostEqual(float(order), 0.90 / 4, places=4)
            self.assertAlmostEqual(
                float(order) * 50, 11.25, places=2
            )

    def test_a_hard_cap_bounds_a_single_order(self) -> None:
        with tempfile.TemporaryDirectory() as name:
            base = _config(Path(name), max_order_hard_pct='"0.05"')
            cfg = Config(**{**base.__dict__, "daily_budget_schedule": SCHEDULE})
            order, _daily = budget_ceilings(cfg, equity=50, confidence=1.0)
            self.assertAlmostEqual(float(order), 0.05, places=4)

    def test_a_config_without_a_schedule_keeps_its_flat_ceilings(self) -> None:
        with tempfile.TemporaryDirectory() as name:
            cfg = _config(Path(name))
            self.assertEqual(cfg.daily_budget_schedule, ())
            order, daily = budget_ceilings(cfg, equity=50, confidence=1.0)
            self.assertEqual(money(daily), money(cfg.daily_notional_pct))
            self.assertEqual(money(order), money(cfg.max_order_pct))


def money(value: Any) -> str:
    return str(Decimal(str(value)).quantize(Decimal("0.000001")))


class ReconcileUnderScheduleTests(unittest.TestCase):
    """The stored budget follows the schedule, and never undoes a cut."""

    def _stored(self, state: Path, **overrides: Any) -> Limits:
        base = {
            "max_order_pct": "0.0096",
            "daily_notional_pct": "0.0386",
            "reason": "evidence_passed",
            "updated_at": "2026-09-18T00:00:00+00:00",
            "confidence": "0.95",
        }
        base.update(overrides)
        limits = Limits(**base)
        save_limits(state, limits)
        return limits

    def test_a_schedule_reprices_a_stale_flat_budget(self) -> None:
        with tempfile.TemporaryDirectory() as name:
            tmp = Path(name)
            base = _config(tmp)
            cfg = Config(
                **{
                    **base.__dict__,
                    "daily_budget_schedule": SCHEDULE,
                    "max_order_pct": Decimal("0.2225"),
                    "daily_notional_pct": Decimal("0.8900"),
                }
            )
            self._stored(Path(cfg.state_dir))
            updated = reconcile(cfg)
            assert updated is not None
            self.assertEqual(money(updated.daily_notional_pct), money("0.89"))
            self.assertEqual(money(updated.max_order_pct), money("0.2225"))
            self.assertEqual(updated.reason, "schedule_applied")

    def test_a_de_risked_budget_is_never_re_priced_up(self) -> None:
        with tempfile.TemporaryDirectory() as name:
            tmp = Path(name)
            base = _config(tmp)
            cfg = Config(
                **{
                    **base.__dict__,
                    "daily_budget_schedule": SCHEDULE,
                    "max_order_pct": Decimal("0.2225"),
                    "daily_notional_pct": Decimal("0.8900"),
                }
            )
            self._stored(
                Path(cfg.state_dir),
                max_order_pct="0.0125",
                daily_notional_pct="0.0500",
                reason="kill_switch",
            )
            updated = reconcile(cfg)
            assert updated is not None
            # The kill switch cut this budget; a schedule is not permission to
            # restore it.
            self.assertEqual(money(updated.daily_notional_pct), money("0.0500"))
            self.assertEqual(money(updated.max_order_pct), money("0.0125"))

    def test_an_unreadable_schedule_row_is_dropped_not_guessed(self) -> None:
        with tempfile.TemporaryDirectory() as name:
            tmp = Path(name)
            first = _config(tmp)
            (tmp / "agentic.toml").write_text(
                (tmp / "agentic.toml").read_text(encoding="utf-8")
                + 'daily_budget_schedule = [[200, "0.9"], ["x", "1"], [500, "0.5", "0.3"]]\n',
                encoding="utf-8",
            )
            cfg = load_config(tmp / "agentic.toml")
            self.assertEqual(cfg.symbol_whitelist, first.symbol_whitelist)
            self.assertEqual(len(cfg.daily_budget_schedule), 2)
            ceiling, _floor = scheduled_daily_bounds(cfg, 100)
            self.assertAlmostEqual(float(ceiling), 0.90, places=4)


if __name__ == "__main__":
    unittest.main()
