"""Backtest honesty, evolution determinism, and the promotion gate."""

from __future__ import annotations

import random
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path

from agentic_trading.backtest import (
    CostModel,
    Genome,
    Metrics,
    bootstrap_p_value,
    fold_metrics,
    run_backtest,
)
from agentic_trading.evolution import evolve
from agentic_trading.history import Bar
from agentic_trading.promotion import (
    Assessment,
    PromotionPolicy,
    PromotionState,
    apply_assessment,
    assess,
    check_demotion,
    load_state,
    save_state,
)


def bar(
    index: int,
    *,
    open_: str,
    high: str,
    low: str,
    close: str,
    symbol: str = "SPY",
) -> Bar:
    start = datetime(2025, 1, 2, 14, 30, tzinfo=timezone.utc) + timedelta(
        minutes=5 * index
    )
    return Bar(
        symbol,
        start,
        Decimal(open_),
        Decimal(high),
        Decimal(low),
        Decimal(close),
    )


def synthetic_bars(
    *, seed: int = 5, count: int = 3000, drift: float = 0.0006, vol: float = 0.0012
) -> list[Bar]:
    rng = random.Random(seed)
    price = 100.0
    bars: list[Bar] = []
    for index in range(count):
        open_ = price
        price = max(1.0, price * (1 + rng.gauss(drift, vol)))
        close = price
        high = max(open_, close) * (1 + abs(rng.gauss(0, vol / 3)))
        low = min(open_, close) * (1 - abs(rng.gauss(0, vol / 3)))
        bars.append(
            bar(
                index,
                open_=f"{open_:.4f}",
                high=f"{high:.4f}",
                low=f"{low:.4f}",
                close=f"{close:.4f}",
            )
        )
    return bars


class BacktestHonestyTests(unittest.TestCase):
    def test_entry_uses_next_bar_open_not_signal_close(self) -> None:
        # Rising closes trigger the signal; the next open is far lower, so an
        # honest backtest must not book the (better) signal-bar price.
        bars = [
            bar(0, open_="100", high="101", low="99", close="100"),
            bar(1, open_="100", high="101", low="99", close="100"),
            bar(2, open_="101", high="102", low="100", close="101"),
            bar(3, open_="102", high="103", low="101", close="102"),
            bar(4, open_="103", high="105", low="102", close="104"),  # signal bar
            bar(5, open_="95", high="96", low="94", close="95"),  # entry at this open
            bar(6, open_="95", high="97", low="94", close="96"),  # timeout exit
            bar(7, open_="96", high="97", low="95", close="96"),
        ]
        genome = Genome(
            lookback=3,
            entry_bps=1,
            tp_bps=1000,
            sl_bps=1000,
            max_hold_bars=1,
            vol_window=3,
            max_vol_bps=100000,
            use_trend_filter=False,
        )
        metrics = run_backtest(
            bars, genome, starting_cash=Decimal("100"), bootstrap_samples=0
        )
        self.assertEqual(metrics.trades, 1)
        trade = metrics.trades_detail[0]
        self.assertEqual(metrics.trades_detail[0].exit_reason, "timeout")
        expected_entry = CostModel().buy_price(Decimal("95"))
        self.assertAlmostEqual(float(trade.entry_price), float(expected_entry), places=6)

    def test_stop_wins_when_a_bar_touches_both_levels(self) -> None:
        bars = [
            bar(0, open_="100", high="101", low="99", close="100"),
            bar(1, open_="100", high="101", low="99", close="101"),
            bar(2, open_="101", high="102", low="100", close="102"),
            bar(3, open_="102", high="103", low="101", close="103"),  # signal
            bar(4, open_="103", high="104", low="102", close="103"),  # entry
            bar(5, open_="103", high="200", low="1", close="103"),  # both touched
            bar(6, open_="103", high="104", low="102", close="103"),
        ]
        genome = Genome(
            lookback=3,
            entry_bps=1,
            tp_bps=50,
            sl_bps=50,
            max_hold_bars=5,
            vol_window=3,
            max_vol_bps=100000,
            use_trend_filter=False,
        )
        metrics = run_backtest(bars, genome, bootstrap_samples=0)
        self.assertEqual(metrics.trades, 1)
        self.assertEqual(metrics.trades_detail[0].exit_reason, "stop")
        self.assertLess(metrics.net_pnl, Decimal("0"))

    def test_costs_reduce_reported_profit(self) -> None:
        bars = synthetic_bars(seed=9, count=400, drift=0.001, vol=0.0008)
        genome = Genome(
            lookback=2,
            entry_bps=5,
            tp_bps=60,
            sl_bps=60,
            max_hold_bars=5,
            vol_window=5,
            max_vol_bps=100000,
            use_trend_filter=False,
        )
        free = run_backtest(
            bars,
            genome,
            costs=CostModel(spread_bps=Decimal("0"), slippage_bps=Decimal("0")),
            bootstrap_samples=0,
        )
        costly = run_backtest(bars, genome, bootstrap_samples=0)
        self.assertEqual(free.trades, costly.trades)
        self.assertGreater(free.net_pnl, costly.net_pnl)

    def test_bootstrap_is_deterministic_and_directional(self) -> None:
        positive = [10.0, 12.0, 8.0, 15.0, 9.0]
        negative = [-10.0, -12.0, -8.0, -15.0, -9.0]
        self.assertLess(bootstrap_p_value(positive, samples=200, seed=1), 0.05)
        self.assertGreater(bootstrap_p_value(negative, samples=200, seed=1), 0.95)
        self.assertEqual(
            bootstrap_p_value(positive, samples=200, seed=1),
            bootstrap_p_value(positive, samples=200, seed=1),
        )
        self.assertEqual(bootstrap_p_value([], samples=200), 1.0)

    def test_empty_sample_has_no_metrics(self) -> None:
        metrics = run_backtest([], Genome(), bootstrap_samples=0)
        self.assertEqual(metrics.trades, 0)
        self.assertEqual(metrics.expectancy_bps, 0.0)
        self.assertFalse(metrics.statistically_positive)

    def test_fold_metrics_split_trades(self) -> None:
        bars = synthetic_bars(seed=11, count=600, drift=0.001, vol=0.0009)
        genome = Genome(
            lookback=2,
            entry_bps=5,
            tp_bps=50,
            sl_bps=50,
            max_hold_bars=4,
            vol_window=5,
            max_vol_bps=100000,
            use_trend_filter=False,
        )
        metrics = run_backtest(bars, genome, bootstrap_samples=0)
        folds = fold_metrics(metrics.trades_detail, folds=3)
        self.assertGreaterEqual(len(folds), 2)
        self.assertEqual(sum(f.trades for f in folds), metrics.trades)


class EvolutionTests(unittest.TestCase):
    def test_evolution_is_deterministic_for_a_seed(self) -> None:
        bars = synthetic_bars(seed=3, count=800, drift=0.0008, vol=0.001)
        first = evolve(bars, population=8, generations=3, seed=17, min_trades=5)
        second = evolve(bars, population=8, generations=3, seed=17, min_trades=5)
        self.assertEqual(first.champion.to_dict(), second.champion.to_dict())
        self.assertEqual(first.out_of_sample.trades, second.out_of_sample.trades)

    def test_evolution_rejects_tiny_samples(self) -> None:
        with self.assertRaises(ValueError):
            evolve(synthetic_bars(count=40), population=4, generations=1)


class PromotionGateTests(unittest.TestCase):
    def _assessment(self, **overrides) -> Assessment:
        base = Assessment(
            eligible=True,
            score=1.0,
            reasons=[],
            evidence={"oos_trades": 50},
        )
        for key, value in overrides.items():
            setattr(base, key, value)
        return base

    def test_gate_refuses_small_out_of_sample_sample(self) -> None:
        bars = synthetic_bars(seed=4, count=900)
        result = evolve(bars, population=6, generations=2, seed=2, min_trades=3)
        # Champion trades few times on this slice: the gate must refuse.
        result.out_of_sample = Metrics(trades=3, expectancy_bps=50.0)
        assessment = assess(result, PromotionPolicy(min_oos_trades=30))
        self.assertFalse(assessment.eligible)
        self.assertTrue(
            any("too small" in reason for reason in assessment.reasons),
            assessment.reasons,
        )

    def test_gate_refuses_noise(self) -> None:
        bars = synthetic_bars(seed=6, count=900, drift=0.0)
        result = evolve(bars, population=8, generations=3, seed=8, min_trades=3)
        assessment = assess(result, PromotionPolicy(min_oos_trades=5))
        self.assertFalse(assessment.eligible)

    def test_gate_accepts_a_strong_out_of_sample_edge(self) -> None:
        bars = synthetic_bars(seed=5, count=2000)
        result = evolve(
            bars, population=12, generations=4, seed=3, min_trades=10, min_oos_trades=15
        )
        assessment = assess(result, PromotionPolicy(min_oos_trades=15))
        self.assertTrue(assessment.eligible, assessment.reasons)

    def test_streak_required_before_promotion(self) -> None:
        policy = PromotionPolicy(required_cycles=2)
        state = PromotionState()
        good = self._assessment()
        self.assertEqual(apply_assessment(state, good, policy), [])
        self.assertEqual(state.stage, "shadow")
        events = apply_assessment(state, good, policy)
        self.assertEqual(state.stage, "probation")
        self.assertEqual(events[0]["event"], "promotion")

    def test_bad_assessment_resets_streak(self) -> None:
        policy = PromotionPolicy(required_cycles=2)
        state = PromotionState(streak=1)
        events = apply_assessment(state, self._assessment(eligible=False), policy)
        self.assertEqual(state.streak, 0)
        self.assertEqual(events[0]["event"], "promotion_streak_reset")

    def test_stage_progression_stops_at_live(self) -> None:
        policy = PromotionPolicy(required_cycles=1)
        state = PromotionState()
        apply_assessment(state, self._assessment(), policy)
        self.assertEqual(state.stage, "probation")
        apply_assessment(state, self._assessment(), policy)
        self.assertEqual(state.stage, "live")
        events = apply_assessment(state, self._assessment(), policy)
        self.assertEqual(state.stage, "live")
        self.assertEqual(events, [])

    def test_demotion_on_kill_switch_errors_and_drawdown(self) -> None:
        policy = PromotionPolicy(demote_drawdown_pct=5.0)
        state = PromotionState(stage="live", stage_equity="100")
        event = check_demotion(
            state,
            policy=policy,
            kill_switch=True,
            consecutive_errors=0,
            current_equity=Decimal("100"),
            max_consecutive_errors=3,
        )
        self.assertEqual(event["reason"], "kill_switch_active")
        self.assertEqual(state.stage, "shadow")

        state = PromotionState(stage="live", stage_equity="100")
        event = check_demotion(
            state,
            policy=policy,
            kill_switch=False,
            consecutive_errors=3,
            current_equity=Decimal("100"),
            max_consecutive_errors=3,
        )
        self.assertEqual(event["reason"], "consecutive_broker_errors")

        state = PromotionState(stage="probation", stage_equity="100")
        event = check_demotion(
            state,
            policy=policy,
            kill_switch=False,
            consecutive_errors=0,
            current_equity=Decimal("90"),
            max_consecutive_errors=3,
        )
        self.assertIn("drawdown", event["reason"])
        self.assertEqual(state.stage, "shadow")

    def test_shadow_is_never_demoted(self) -> None:
        state = PromotionState(stage="shadow")
        self.assertIsNone(
            check_demotion(
                state,
                policy=PromotionPolicy(),
                kill_switch=True,
                consecutive_errors=9,
                current_equity=Decimal("1"),
                max_consecutive_errors=3,
            )
        )

    def test_state_roundtrip(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            state = PromotionState(stage="probation", streak=2, stage_equity="55")
            save_state(tmp, state)
            loaded = load_state(tmp)
            self.assertEqual(loaded.stage, "probation")
            self.assertEqual(loaded.streak, 2)
            self.assertEqual(loaded.stage_equity, "55")

    def test_corrupt_state_file_falls_back_to_shadow(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "promotion.json"
            path.write_text("{not json", encoding="utf-8")
            self.assertEqual(load_state(tmp).stage, "shadow")


if __name__ == "__main__":
    unittest.main()
