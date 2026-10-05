"""The birth screen: an in-sample filter on bars before birth, plus a near-duplicate check."""

from __future__ import annotations

import unittest
from dataclasses import replace
from datetime import timedelta
from decimal import Decimal
from unittest import mock

from agentic_trading.backtest import CostModel
from agentic_trading.swarm.life import Agent
from agentic_trading.swarm.recipe import validate
from agentic_trading.swarm.screen import correlation, duplicate_of, screen
from tests.swarm_support import D0, universe

TREND = validate({"family": "trend", "params": {"horizons": [10, 20, 50, 100], "min_vote": 0.5, "max_positions": 4},
                  "universe": "all", "per_order_pct": 0.2, "inverse_vol": False})
BIRTH = D0 + timedelta(days=400)


def _fake(trades: int, curve: list[float]):
    return mock.patch("agentic_trading.swarm.screen.simulate", return_value=([{}] * trades, curve))


class ScreenTests(unittest.TestCase):
    def test_bars_on_or_after_birth_change_nothing(self) -> None:
        series = universe(420)
        before = screen(TREND, series, BIRTH, costs=CostModel(), cash=50)
        spiked = dict(series)
        spiked["BTCUSD"] = [replace(b, close=b.close * Decimal("50")) if b.start.date() >= BIRTH else b
                            for b in series["BTCUSD"]]
        self.assertEqual(screen(TREND, spiked, BIRTH, costs=CostModel(), cash=50), before)
        self.assertTrue(all(day < BIRTH.isoformat() for day in before.signature))

    def test_each_threshold_on_both_sides(self) -> None:
        good = [50.0] + [50.0 + i * 0.1 for i in range(1, 40)] + [54.0]
        cases = [
            (20, good, True, "passed"),
            (19, good, False, "only 19 trades"),
            (20, [50.0, 50.0, 49.0, 49.9], False, "lost"),
            (20, [50.0, 60.0, 42.1, 61.0], True, "passed"),  # a 29.8% fall is inside the limit
            (20, [50.0, 60.0, 41.9, 61.0], False, "drawdown 30%"),  # 30.2% is not
        ]
        for trades, curve, passed, needle in cases:
            with self.subTest(trades=trades, curve=curve[:4]), _fake(trades, curve):
                result = screen(TREND, universe(60), BIRTH, costs=CostModel(), cash=50)
                self.assertEqual(result.passed, passed, result.reason)
                self.assertIn(needle, result.reason)

    def test_no_history_fails_plainly(self) -> None:
        with _fake(0, []):
            self.assertIn("no history", screen(TREND, universe(10), BIRTH, costs=CostModel(), cash=50).reason)


class DuplicateTests(unittest.TestCase):
    def test_correlation_needs_overlap(self) -> None:
        a = {f"2025-01-{d:02d}": (d % 7 - 3) / 100 for d in range(1, 32)}
        self.assertIsNone(correlation(a, a))  # 31 shared days < 60
        long = {f"d{i:03d}": ((i * 7) % 11 - 5) / 100 for i in range(100)}
        self.assertAlmostEqual(correlation(long, long), 1.0)
        self.assertAlmostEqual(correlation(long, {k: -v for k, v in long.items()}), -1.0)
        self.assertIsNone(correlation(long, {k: 0.0 for k in long}))  # no variance

    def test_a_near_copy_of_a_living_agent_is_named(self) -> None:
        long = {f"d{i:03d}": ((i * 7) % 11 - 5) / 100 for i in range(100)}
        living = [Agent(TREND, "2025-01-01", long)]
        self.assertEqual(duplicate_of({k: v * 1.01 for k, v in long.items()}, living), TREND.name)
        self.assertIsNone(duplicate_of({k: -v for k, v in long.items()}, living))
