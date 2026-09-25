"""The allocator: capital only for members beating buy-and-hold on live results."""

from __future__ import annotations

import unittest
from datetime import date, timedelta

from agentic_trading.desk.allocator import MemberRecord, allocate

START = date(2026, 9, 24)


def series(returns: list[float], start: float = 50.0) -> list[tuple[str, float]]:
    out, equity = [(START.isoformat(), start)], start
    for day, r in enumerate(returns, start=1):
        equity *= 1 + r
        out.append(((START + timedelta(days=day)).isoformat(), equity))
    return out


FLAT = [0.0] * 25
BENCH = MemberRecord("benchmark", series([0.001] * 25), entries=2)
# Beats the benchmark by a steady but noisy margin: t well above 1.
WINNER = [0.004 if i % 2 else 0.0015 for i in range(25)]


class AllocatorTests(unittest.TestCase):
    def test_no_evidence_means_everything_holds_the_benchmark(self) -> None:
        result = allocate(
            [BENCH, MemberRecord("rot", series(WINNER[:18]), entries=3)],
            benchmark="benchmark",
            previous={},
        )
        self.assertEqual(result.weights, {"benchmark": 1.0, "rot": 0.0})
        self.assertIn("19 daily samples", result.reasons["rot"])

    def test_twenty_samples_and_an_edge_qualify_capped_at_sixty_percent(self) -> None:
        result = allocate(
            [BENCH, MemberRecord("rot", series(WINNER[:19]), entries=3)],
            benchmark="benchmark",
            previous={"benchmark": 1.0},
        )
        self.assertEqual(result.weights, {"benchmark": 0.4, "rot": 0.6})
        self.assertTrue(result.changed)
        self.assertIn("qualifies", result.reasons["rot"])

    def test_a_member_with_no_entries_cannot_qualify(self) -> None:
        result = allocate(
            [BENCH, MemberRecord("rot", series(WINNER), entries=0)],
            benchmark="benchmark",
            previous={},
        )
        self.assertEqual(result.weights["rot"], 0.0)
        self.assertIn("no entries", result.reasons["rot"])

    def test_trailing_the_benchmark_disqualifies(self) -> None:
        result = allocate(
            [BENCH, MemberRecord("flat", series(FLAT), entries=5)],
            benchmark="benchmark",
            previous={},
        )
        self.assertEqual(result.weights["flat"], 0.0)
        self.assertIn("trails the benchmark", result.reasons["flat"])

    def test_a_lucky_single_day_does_not_pass_the_consistency_bar(self) -> None:
        lucky = [0.0] * 24 + [0.10]  # beats in total, t well below 1
        result = allocate(
            [BENCH, MemberRecord("lucky", series(lucky), entries=1)],
            benchmark="benchmark",
            previous={},
        )
        self.assertEqual(result.weights["lucky"], 0.0)
        self.assertIn("t=", result.reasons["lucky"])

    def test_weights_split_by_t_between_qualifiers(self) -> None:
        strong = [0.006 if i % 2 else 0.003 for i in range(25)]
        result = allocate(
            [
                BENCH,
                MemberRecord("a", series(WINNER), entries=3),
                MemberRecord("b", series(strong), entries=3),
            ],
            benchmark="benchmark",
            previous={},
        )
        self.assertAlmostEqual(sum(result.weights.values()), 1.0, places=6)
        self.assertGreater(result.weights["a"], 0.0)
        self.assertGreater(result.weights["b"], 0.0)
        self.assertLessEqual(max(result.weights["a"], result.weights["b"]), 0.6)

    def test_small_changes_keep_the_previous_allocation(self) -> None:
        previous = {"benchmark": 0.45, "rot": 0.55}
        result = allocate(
            [BENCH, MemberRecord("rot", series(WINNER), entries=3)],
            benchmark="benchmark",
            previous=previous,
        )
        # The new weights would be 0.4 / 0.6, a 5-point move: under hysteresis.
        self.assertEqual(result.weights, previous)
        self.assertFalse(result.changed)
