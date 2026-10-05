"""The rankers take their parameters from the caller; the defaults are today's rules."""

from __future__ import annotations

import unittest
from datetime import datetime, timezone

from agentic_trading.walkforward import (
    REVERSAL_BOOK, ROTATION_BOOKS, rank_reversal, rank_rotation, simulate, targets_as_of,
)
from tests.swarm_support import universe

WHEN = datetime(2025, 2, 3, tzinfo=timezone.utc)  # a Monday, ~400 bars in


class DefaultParityTests(unittest.TestCase):
    def test_explicit_defaults_select_exactly_what_no_arguments_select(self) -> None:
        series = universe()
        self.assertEqual(rank_rotation(series, WHEN), rank_rotation(series, WHEN, books=ROTATION_BOOKS))
        self.assertEqual(rank_reversal(series, WHEN), rank_reversal(series, WHEN, book=REVERSAL_BOOK))
        for rule in ("trend", "rotation", "reversal"):
            with self.subTest(rule=rule):
                self.assertEqual(targets_as_of(series, WHEN, rule=rule),
                                 targets_as_of(series, WHEN, rule=rule, books=None, reversal=None))


class ParameterTests(unittest.TestCase):
    def test_rotation_top_and_lookback_come_from_the_caller(self) -> None:
        series = universe()
        books = {"equity": ("SPY", 50, 20, 1), "crypto": ("BTCUSD", 50, 20, 1)}
        chosen = [r["symbol"] for r in rank_rotation(series, WHEN, books=books) if r["selected"]]
        self.assertLessEqual(len(chosen), 2)  # one per book at most
        default = [r["symbol"] for r in rank_rotation(series, WHEN) if r["selected"]]
        self.assertGreaterEqual(len(default), len(chosen))

    def test_reversal_top_comes_from_the_caller(self) -> None:
        series = universe()
        rows = rank_reversal(series, WHEN, book=("SPY", 50, 50, 5, 1))
        self.assertLessEqual(sum(1 for r in rows if r["selected"]), 1)

    def test_simulate_passes_trend_horizons_through(self) -> None:
        series = universe(200)  # too short for the default 252-bar horizon
        start = datetime(2024, 4, 1, tzinfo=timezone.utc)
        end = datetime(2024, 7, 1, tzinfo=timezone.utc)
        none, _ = simulate(series, start=start, end=end, rule="trend", per_order_pct=0.2, max_position_pct=0.2)
        some, _ = simulate(series, start=start, end=end, rule="trend", per_order_pct=0.2, max_position_pct=0.2,
                           horizons=(5, 10, 20, 40))
        self.assertEqual(none, [])
        self.assertGreater(len(some), 0)
