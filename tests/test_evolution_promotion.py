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
    assess_walkforward,
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
    def test_gate_tightens_with_the_number_of_hypotheses_searched(self) -> None:
        """Same evidence, same p-value: more search must mean a higher bar."""
        from agentic_trading.backtest import Metrics
        from agentic_trading.evolution import EvolutionResult

        def result(evaluated: int) -> EvolutionResult:
            return EvolutionResult(
                champion=Genome(),
                in_sample=Metrics(trades=60, expectancy_bps=30.0, max_drawdown_pct=4.0),
                out_of_sample=Metrics(
                    trades=60,
                    win_rate=0.6,
                    expectancy_bps=25.0,
                    profit_factor=1.8,
                    max_drawdown_pct=5.0,
                    bootstrap_p_value=0.01,
                ),
                oos_folds=[Metrics(trades=20, expectancy_bps=20.0)] * 3,
                evaluated=evaluated,
            )

        policy = PromotionPolicy(min_oos_trades=30)
        single = assess(result(1), policy)
        searched = assess(result(400), policy)

        self.assertTrue(single.eligible, single.reasons)
        self.assertFalse(searched.eligible)
        self.assertTrue(
            any("does not survive the search" in reason for reason in searched.reasons),
            searched.reasons,
        )
        self.assertAlmostEqual(
            searched.evidence["effective_alpha"], 0.05 / 400, places=6
        )

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
            bars, population=12, generations=4, seed=3, min_trades=10, min_oos_trades=10
        )
        assessment = assess(result, PromotionPolicy(min_oos_trades=10))
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


class WalkForwardGateTests(unittest.TestCase):
    """The pre-registered test is the one the gate answers to.

    Search divides its significance bar by every genome tried; the fixed rule
    was declared before the numbers were seen, so it pays no such penalty — but
    it must still clear the same drawdown, fold and sample requirements, and the
    book actually traded has to fit inside the measured drawdown ceiling.
    """

    def _report(self, **overrides) -> dict:
        now = datetime.now(timezone.utc).isoformat()
        production = {
            "per_order_pct": 0.01,
            "trades": 495,
            "expectancy_bps": 855.5,
            "max_drawdown_pct": 13.96,
            "bootstrap_p_value": 0.0005,
            "profit_factor": 3.69,
            "win_rate": 0.5,
            "eligible": True,
            "folds": [
                {"index": 0, "expectancy_bps": 38769},
                {"index": 1, "expectancy_bps": 4203},
                {"index": 2, "expectancy_bps": 6590},
                {"index": 3, "expectancy_bps": -186},
                {"index": 4, "expectancy_bps": 154},
                {"index": 5, "expectancy_bps": 46},
            ],
        }
        production.update(overrides.pop("production", {}))
        report = {
            "generated_at": now,
            "drawdown_ceiling_pct": 15.0,
            "series": {"symbols": ["SPY", "BTC-USD"], "bars": 1000},
            "configs": {"production": production, "inverse_vol": {}},
            "gate_size": {
                "per_order_pct": 0.01,
                "max_drawdown_pct": 13.96,
                "eligible": True,
            },
        }
        report.update(overrides)
        return report

    def test_a_passing_report_is_eligible_without_a_search_penalty(self) -> None:
        assessment = assess_walkforward(
            self._report(), PromotionPolicy(), live_per_order_pct=0.01
        )
        self.assertTrue(assessment.eligible, assessment.reasons)
        self.assertEqual(assessment.evidence["source"], "walkforward")
        self.assertEqual(assessment.evidence["tested_hypotheses"], 1)
        self.assertEqual(
            assessment.evidence["effective_alpha"],
            PromotionPolicy().max_bootstrap_p_value,
        )
        self.assertGreater(assessment.confidence, 0.5)

    def test_trading_more_than_the_evidence_supports_blocks_promotion(self) -> None:
        assessment = assess_walkforward(
            self._report(), PromotionPolicy(), live_per_order_pct=0.05
        )
        self.assertFalse(assessment.eligible)
        self.assertTrue(
            any("only supports" in reason for reason in assessment.reasons),
            assessment.reasons,
        )

    def test_a_stale_report_cannot_promote(self) -> None:
        old = (datetime.now(timezone.utc) - timedelta(days=90)).isoformat()
        assessment = assess_walkforward(
            self._report(generated_at=old), PromotionPolicy(), live_per_order_pct=0.01
        )
        self.assertFalse(assessment.eligible)
        self.assertTrue(any("days old" in r for r in assessment.reasons))

    def test_drawdown_over_the_ceiling_blocks_promotion(self) -> None:
        assessment = assess_walkforward(
            self._report(production={"max_drawdown_pct": 30.5}),
            PromotionPolicy(),
            live_per_order_pct=0.01,
        )
        self.assertFalse(assessment.eligible)
        self.assertTrue(any("drawdown" in r for r in assessment.reasons))

    def test_a_losing_fold_majority_blocks_promotion(self) -> None:
        assessment = assess_walkforward(
            self._report(
                production={
                    "folds": [
                        {"index": 0, "expectancy_bps": -10},
                        {"index": 1, "expectancy_bps": -20},
                        {"index": 2, "expectancy_bps": 30},
                    ]
                }
            ),
            PromotionPolicy(),
            live_per_order_pct=0.01,
        )
        self.assertFalse(assessment.eligible)
        self.assertTrue(any("folds profitable" in r for r in assessment.reasons))

    def test_no_size_fits_the_ceiling_blocks_promotion(self) -> None:
        assessment = assess_walkforward(
            self._report(gate_size={"per_order_pct": None, "eligible": False}),
            PromotionPolicy(),
            live_per_order_pct=0.01,
        )
        self.assertFalse(assessment.eligible)
        self.assertTrue(any("no size holds" in r for r in assessment.reasons))

    def test_an_unreadable_timestamp_is_not_treated_as_fresh(self) -> None:
        assessment = assess_walkforward(
            self._report(generated_at="not-a-date"),
            PromotionPolicy(),
            live_per_order_pct=0.01,
        )
        self.assertFalse(assessment.eligible)
        self.assertTrue(any("timestamp" in r for r in assessment.reasons))


class PrimaryEvidenceTests(unittest.TestCase):
    """Which experiment the daemon's promotion cycle answers to."""

    def _config(self, tmp: Path):
        from agentic_trading.config import load_config

        (tmp / "state").mkdir(parents=True, exist_ok=True)
        (tmp / "journal").mkdir(parents=True, exist_ok=True)
        (tmp / "bars").mkdir(parents=True, exist_ok=True)
        cfg = tmp / "agentic.toml"
        cfg.write_text(
            "\n".join(
                [
                    'mode = "shadow"',
                    'symbol_whitelist = ["SPY"]',
                    'max_order_pct = "0.05"',
                    'daily_notional_pct = "0.20"',
                    'daily_loss_pct = "0.03"',
                    "max_open_positions = 1",
                    "equity_refresh_ticks = 30",
                    "equity_refresh_seconds = 60",
                    'timezone = "local"',
                    f'history_path = "{tmp / "bars"}"',
                    f'quotes_path = "{tmp / "q.jsonl"}"',
                    f'journal_dir = "{tmp / "journal"}"',
                    f'state_dir = "{tmp / "state"}"',
                    f'tools_snapshot_path = "{tmp / "tools.json"}"',
                    f'token_path = "{tmp / "tok.json"}"',
                    'mcp_url = "https://agent.robinhood.com/mcp/trading"',
                ]
            )
            + "\n",
            encoding="utf-8",
        )
        return load_config(cfg)

    def test_falls_back_to_the_search_when_no_report_exists(self) -> None:
        from agentic_trading.selfimprove import _primary_assessment

        with tempfile.TemporaryDirectory() as name:
            config = self._config(Path(name))
            search = Assessment(eligible=False, score=1.0, reasons=["search"])
            chosen = _primary_assessment(config, PromotionPolicy(), search)
        self.assertIs(chosen, search)

    def test_uses_the_walk_forward_report_when_there_is_one(self) -> None:
        import json as _json

        from agentic_trading.selfimprove import _primary_assessment

        with tempfile.TemporaryDirectory() as name:
            tmp = Path(name)
            config = self._config(tmp)
            report = WalkForwardGateTests()._report()
            (tmp / "state" / "strategy_evidence.json").write_text(
                _json.dumps(report)
            )
            # The ladder has to be inside the measured ceiling, or the gate
            # refuses on size regardless of how good the rule looks.
            from agentic_trading.limits import Limits, save_limits

            save_limits(
                tmp / "state",
                Limits(
                    max_order_pct="0.01",
                    daily_notional_pct="0.04",
                    reason="test",
                    updated_at=datetime.now(timezone.utc).isoformat(),
                ),
            )
            search = Assessment(eligible=False, score=1.0, reasons=["search"])
            chosen = _primary_assessment(config, PromotionPolicy(), search)
        self.assertIsNot(chosen, search)
        self.assertEqual(chosen.evidence["source"], "walkforward")
        self.assertTrue(chosen.eligible, chosen.reasons)


class EvidenceKeyTests(unittest.TestCase):
    """Re-running the report must not look like new evidence."""

    def test_key_changes_with_the_numbers_and_not_with_the_clock(self) -> None:
        from agentic_trading.promotion import assess_walkforward

        base = WalkForwardGateTests()._report()
        first = assess_walkforward(base, PromotionPolicy(), live_per_order_pct=0.01)
        # Same numbers, later timestamp: same evidence.
        relabelled = dict(base)
        relabelled["generated_at"] = datetime.now(timezone.utc).isoformat()
        second = assess_walkforward(
            relabelled, PromotionPolicy(), live_per_order_pct=0.01
        )
        self.assertEqual(
            first.evidence["report_key"], second.evidence["report_key"]
        )

        # A different measurement is different evidence.
        changed = WalkForwardGateTests()._report(production={"trades": 496})
        third = assess_walkforward(
            changed, PromotionPolicy(), live_per_order_pct=0.01
        )
        self.assertNotEqual(
            first.evidence["report_key"], third.evidence["report_key"]
        )

    def test_a_lowered_ceiling_makes_the_size_claim_new_evidence(self) -> None:
        from agentic_trading.promotion import assess_walkforward

        generous = WalkForwardGateTests()._report()
        tight = WalkForwardGateTests()._report(
            gate_size={"per_order_pct": 0.005, "max_drawdown_pct": 9.0}
        )
        a = assess_walkforward(generous, PromotionPolicy(), live_per_order_pct=0.01)
        b = assess_walkforward(tight, PromotionPolicy(), live_per_order_pct=0.01)
        self.assertNotEqual(a.evidence["report_key"], b.evidence["report_key"])
        self.assertFalse(b.eligible, "0.5% of headroom cannot carry a 1% book")
