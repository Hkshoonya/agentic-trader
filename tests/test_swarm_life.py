"""An agent's record: forward only, same engine as the screen, judged against 60/40."""

from __future__ import annotations

import unittest
from datetime import date, timedelta

from agentic_trading.backtest import CostModel
from agentic_trading.swarm.life import (
    Agent, Record, agent_weights, benchmark_returns, forward_record, through, window_dates,
)
from agentic_trading.swarm.recipe import validate
from agentic_trading.walkforward import simulate
from tests.swarm_support import D0, daily, universe

TREND = validate({"family": "trend", "params": {"horizons": [10, 20, 50, 100], "min_vote": 0.5, "max_positions": 4},
                  "universe": "all", "per_order_pct": 0.2, "inverse_vol": False})
BIRTH = D0 + timedelta(days=300)


class LifeTests(unittest.TestCase):
    def test_through_drops_every_bar_after_the_day(self) -> None:
        series = through(universe(40), D0 + timedelta(days=9))
        self.assertTrue(all(b.start.date() <= D0 + timedelta(days=9) for bars in series.values() for b in bars))
        self.assertEqual(len(series["BTCUSD"]), 10)

    def test_window_dates_are_the_days_simulate_walks(self) -> None:
        from agentic_trading.swarm.life import as_datetime
        series = universe(200)
        start, end = D0 + timedelta(days=120), D0 + timedelta(days=180)
        _, curve = simulate(series, start=as_datetime(start), end=as_datetime(end), costs=CostModel())
        self.assertEqual(len(curve), len(window_dates(series, start, end)) + 2)

    def test_the_record_starts_at_birth_and_is_repeatable(self) -> None:
        series = universe(420)
        as_of = BIRTH + timedelta(days=60)
        record = forward_record(TREND, series, BIRTH, as_of, costs=CostModel(), cash=50)
        self.assertEqual(record.days[0], BIRTH.isoformat())
        self.assertLessEqual(record.days[-1], as_of.isoformat())
        self.assertEqual(len(record.returns), len(record.excess))
        self.assertEqual(record, forward_record(TREND, series, BIRTH, as_of, costs=CostModel(), cash=50))
        self.assertEqual(forward_record(TREND, series, BIRTH, BIRTH - timedelta(days=1), costs=CostModel(), cash=50),
                         Record())

    def test_upto_keeps_only_earlier_days(self) -> None:
        record = forward_record(TREND, universe(420), BIRTH, BIRTH + timedelta(days=40), costs=CostModel(), cash=50)
        cut = record.upto((BIRTH + timedelta(days=10)).isoformat())
        self.assertEqual(len(cut.days), 10)
        self.assertTrue(all(d < (BIRTH + timedelta(days=10)).isoformat() for d in cut.days))

    def test_the_benchmark_is_sixty_forty_and_a_closed_market_counts_zero(self) -> None:
        saturday = date(2024, 1, 6)
        series = {"QQQ": daily("QQQ", [100, 101], start=date(2024, 1, 4)),  # Thu, Fri
                  "BTCUSD": daily("BTCUSD", [100, 102, 104], start=date(2024, 1, 4))}  # Thu, Fri, Sat
        friday, = benchmark_returns(series, [date(2024, 1, 5)])
        self.assertAlmostEqual(friday, 0.6 * 0.01 + 0.4 * 0.02)
        weekend, = benchmark_returns(series, [saturday])
        self.assertAlmostEqual(weekend, 0.4 * (104 / 102 - 1))

    def test_agent_weights_follow_the_sizing_and_never_exceed_the_book(self) -> None:
        day = D0 + timedelta(days=400)
        weights = agent_weights(TREND, universe(420), day)
        self.assertTrue(weights)
        self.assertTrue(all(abs(w - 0.2) < 1e-9 for w in weights.values()))
        self.assertLessEqual(sum(weights.values()), 1.0 + 1e-9)
        crypto = validate({**TREND.content(), "universe": "crypto"})
        self.assertTrue(all(s.endswith("USD") for s in agent_weights(crypto, universe(420), day)))

    def test_a_rotation_without_its_regime_symbol_holds_nothing_in_that_book(self) -> None:
        rotation = validate({"family": "rotation", "universe": "crypto", "per_order_pct": 0.2, "inverse_vol": False,
                             "params": {"equity_ma": 50, "equity_lookback": 20, "equity_top": 3, "crypto_ma": 50,
                                        "crypto_lookback": 20, "crypto_top": 2, "max_positions": 5}})
        weights = agent_weights(rotation, universe(420), D0 + timedelta(days=400))
        self.assertTrue(all(s.endswith("USD") for s in weights))

    def test_an_agent_row_round_trips(self) -> None:
        agent = Agent(TREND, BIRTH.isoformat(), {"2024-10-01": 0.01})
        again = Agent.from_row(agent.to_row())
        self.assertEqual((again.recipe.id, again.born, again.signature, again.errored),
                         (TREND.id, BIRTH.isoformat(), {"2024-10-01": 0.01}, ""))


class CompoundingTests(unittest.TestCase):
    def test_excess_compounds_like_the_desk(self) -> None:
        record = Record(("d1", "d2"), (0.1, 0.1), (0.1, 0.1), 2, 0.0, 21.0)  # the benchmark stood still
        self.assertAlmostEqual(record.excess_pct, 21.0)
        flat = Record(("d1", "d2"), (0.1, -0.1), (0.0, 0.0), 2, 0.0, -1.0)  # matched the benchmark exactly
        self.assertAlmostEqual(flat.excess_pct, 0.0)
