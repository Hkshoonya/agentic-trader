"""Who earns a share of the swarm's book, and who dies."""

from __future__ import annotations

import unittest
from datetime import date
from unittest import mock

from agentic_trading.swarm.blend import BENCHMARK_WEIGHTS, blend_weights, contributing, cull, shares
from agentic_trading.swarm.life import Agent, Record
from agentic_trading.swarm.recipe import validate

BASE = {"family": "trend", "params": {"horizons": [10, 20, 50, 100], "min_vote": 0.5, "max_positions": 4},
        "universe": "all", "per_order_pct": 0.2, "inverse_vol": False}


def _agent(days: int, daily_excess: float, *, vol: float = 0.01, drawdown: float = 0.0, per_order: float = 0.2,
           errored: str = "") -> Agent:
    returns = tuple(vol if i % 2 else -vol for i in range(days))
    record = Record(tuple(f"d{i:04d}" for i in range(days)), returns, tuple([daily_excess] * days), 10, drawdown, 0.0)
    recipe = validate({**BASE, "per_order_pct": per_order})
    return Agent(recipe, "2025-01-01", {}, errored, record)


MONDAY, TUESDAY = date(2026, 10, 5), date(2026, 10, 6)


class ContributingTests(unittest.TestCase):
    def test_the_nursery_negative_excess_and_errors_get_nothing(self) -> None:
        self.assertFalse(contributing(_agent(19, 0.001), nursery_days=20))
        self.assertTrue(contributing(_agent(20, 0.001), nursery_days=20))
        self.assertFalse(contributing(_agent(80, -0.001), nursery_days=20))
        self.assertFalse(contributing(_agent(80, 0.001, errored="boom"), nursery_days=20))

    def test_shares_are_inverse_volatility_and_sum_to_one(self) -> None:
        calm = _agent(30, 0.001, vol=0.01, per_order=0.1)
        wild = _agent(30, 0.001, vol=0.03, per_order=0.25)
        got = shares([calm, wild, _agent(5, 0.01)], nursery_days=20)
        self.assertAlmostEqual(sum(got.values()), 1.0)
        self.assertAlmostEqual(got[calm.recipe.id] / got[wild.recipe.id], 3.0)

    def test_no_contributor_means_the_benchmark(self) -> None:
        self.assertEqual(blend_weights([_agent(5, 0.01)], {}, {}, MONDAY), BENCHMARK_WEIGHTS)

    def test_the_blend_is_the_share_weighted_sum_of_agent_targets(self) -> None:
        a, b = _agent(30, 0.001, per_order=0.1), _agent(30, 0.001, per_order=0.25)
        fake = {a.recipe.id: {"SPY": 0.5, "BTCUSD": 0.5}, b.recipe.id: {"BTCUSD": 1.0}}
        with mock.patch("agentic_trading.swarm.blend.agent_weights", side_effect=lambda r, s, d: fake[r.id]):
            got = blend_weights([a, b], {a.recipe.id: 0.25, b.recipe.id: 0.75}, {}, MONDAY)
        self.assertEqual(got, {"SPY": 0.125, "BTCUSD": 0.875})


class CullTests(unittest.TestCase):
    def test_a_deep_drawdown_dies_any_day(self) -> None:
        dying = _agent(10, 0.0, drawdown=26.0)
        [(agent, reason)] = cull([dying, _agent(10, 0.0, drawdown=10.0, per_order=0.1)], today=TUESDAY,
                                 max_drawdown_pct=25.0, cull_after_days=60)
        self.assertIs(agent, dying)
        self.assertIn("drawdown 26%", reason)

    def test_the_bottom_quarter_dies_on_mondays_only_and_only_when_losing(self) -> None:
        agents = [_agent(70, e, per_order=p) for e, p in ((-0.002, 0.1), (-0.001, 0.15), (0.001, 0.2),
                                                           (0.002, 0.25), (0.003, 0.33))]
        self.assertEqual(cull(agents, today=TUESDAY, max_drawdown_pct=25.0, cull_after_days=60), [])
        [(agent, reason)] = cull(agents, today=MONDAY, max_drawdown_pct=25.0, cull_after_days=60)
        self.assertIs(agent, agents[0])
        self.assertIn("bottom quarter", reason)
        winners = [_agent(70, 0.001, per_order=p) for p in (0.1, 0.15, 0.2, 0.25)]
        self.assertEqual(cull(winners, today=MONDAY, max_drawdown_pct=25.0, cull_after_days=60), [])

    def test_fewer_than_four_mature_agents_are_never_quartered(self) -> None:
        agents = [_agent(70, -0.001, per_order=p) for p in (0.1, 0.15, 0.2)] + [_agent(30, -0.01, per_order=0.25)]
        self.assertEqual(cull(agents, today=MONDAY, max_drawdown_pct=25.0, cull_after_days=60), [])
