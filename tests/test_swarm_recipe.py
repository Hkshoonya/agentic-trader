"""Recipes are data: validated, hashed, and turned into walk-forward arguments."""

from __future__ import annotations

import tempfile
import unittest
from decimal import Decimal
from pathlib import Path

from agentic_trading.swarm.recipe import (
    Recipe, rule_kwargs, sim_kwargs, universe_series, validate,
)
from agentic_trading.swarm.settings import SwarmConfig, load_swarm_config
from tests.swarm_support import universe

TREND = {"family": "trend", "params": {"horizons": [20, 50, 100, 200], "min_vote": 0.75, "max_positions": 3},
         "universe": "all", "per_order_pct": 0.2, "inverse_vol": True}


def _load(text: str) -> SwarmConfig:
    with tempfile.TemporaryDirectory() as name:
        path = Path(name) / "agentic.toml"
        path.write_text(text, encoding="utf-8")
        return load_swarm_config(path)


class SettingsTests(unittest.TestCase):
    def test_off_by_default_with_the_spec_numbers(self) -> None:
        config = _load("")
        self.assertFalse(config.enabled)
        self.assertEqual((config.max_agents, config.screens_per_day, config.nursery_days, config.cull_after_days),
                         (24, 12, 20, 60))
        self.assertEqual(config.max_drawdown, Decimal("0.25"))
        self.assertEqual((config.llm_scout, config.llm_proposals_per_week, config.seed), (False, 3, 7))

    def test_mistakes_are_refused_with_the_key_named(self) -> None:
        for text, needle in {'[swarm]\nenabeld = true\n': "unknown keys",
                             '[swarm]\nenabled = "true"\n': "swarm.enabled",
                             '[swarm]\nmax_agents = 0\n': "swarm.max_agents",
                             '[swarm]\nmax_drawdown = "0.9"\n': "swarm.max_drawdown",
                             '[swarm]\nllm_scout = 1\n': "swarm.llm_scout"}.items():
            with self.subTest(text=text), self.assertRaisesRegex(ValueError, needle):
                _load(text)


class RecipeTests(unittest.TestCase):
    def test_a_valid_recipe_round_trips_and_its_id_is_its_content(self) -> None:
        recipe = validate(TREND)
        self.assertEqual(validate(recipe.to_dict()).id, recipe.id)
        self.assertEqual(len(recipe.id), 8)
        self.assertTrue(recipe.name.startswith("trend-"))
        other = validate({**TREND, "per_order_pct": 0.25})
        self.assertNotEqual(other.id, recipe.id)
        self.assertEqual(validate(TREND, origin="scout").id, recipe.id)  # origin is not content

    def test_bad_recipes_are_refused_with_the_problem_named(self) -> None:
        cases = {
            "family": {**TREND, "family": "astrology"},
            "universe": {**TREND, "universe": "forex"},
            "min_vote": {**TREND, "params": {**TREND["params"], "min_vote": 0.6}},
            "unknown": {**TREND, "params": {**TREND["params"], "leverage": 3}},
            "missing": {**TREND, "params": {"min_vote": 0.75}},
            "per_order_pct": {**TREND, "per_order_pct": 0.9},
            "equities only": {"family": "reversal", "params": {"regime_ma": 200, "trend_ma": 200, "lookback": 5,
                                                               "top": 3, "max_positions": 3},
                              "universe": "crypto", "per_order_pct": 0.2, "inverse_vol": False},
        }
        for needle, raw in cases.items():
            with self.subTest(needle=needle), self.assertRaisesRegex(ValueError, needle):
                validate(raw)

    def test_walk_forward_arguments_per_family(self) -> None:
        trend = validate(TREND)
        self.assertEqual(rule_kwargs(trend), {"rule": "trend", "horizons": (20, 50, 100, 200), "min_vote": 0.75,
                                              "max_positions": 3})
        self.assertEqual(sim_kwargs(trend)["per_order_pct"], 0.2)
        self.assertEqual(sim_kwargs(trend)["max_position_pct"], 0.2)
        self.assertTrue(sim_kwargs(trend)["inverse_vol"])
        rotation = validate({"family": "rotation", "universe": "all", "per_order_pct": 0.2, "inverse_vol": False,
                             "params": {"equity_ma": 200, "equity_lookback": 63, "equity_top": 3, "crypto_ma": 100,
                                        "crypto_lookback": 60, "crypto_top": 2, "max_positions": 5}})
        self.assertEqual(rule_kwargs(rotation)["books"],
                         {"equity": ("SPY", 200, 63, 3), "crypto": ("BTCUSD", 100, 60, 2)})

    def test_universe_slices(self) -> None:
        series = universe(30)
        self.assertEqual(set(universe_series(series, "crypto")), {"BTCUSD", "ETHUSD", "SOLUSD"})
        self.assertEqual(set(universe_series(series, "equity")), {"SPY", "QQQ", "AAPL", "MSFT"})
        self.assertEqual(len(universe_series(series, "all")), 7)
