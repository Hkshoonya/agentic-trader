"""Concentration limits and measured execution costs."""

from __future__ import annotations

import json
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path

from agentic_trading.backtest import CostModel
from agentic_trading.correlation import (
    CorrelationState,
    compute_state,
    correlation,
    correlated_with,
    load_state,
    returns_from_closes,
    save_state,
)
from agentic_trading.execution import (
    CostReport,
    cost_model_for,
    decision_prices,
    load_report,
    measure,
    parse_trade_history,
    save_report,
)
from agentic_trading.risk import RiskGuard, PortfolioSnapshot
from agentic_trading.types import OrderIntent, Side


def guard(**kwargs) -> RiskGuard:
    risk = RiskGuard(
        mode="shadow",
        whitelist=["BTC-USD", "SOL-USD", "SPY", "AAPL", "QQQ"],
        max_order_pct=Decimal("0.05"),
        daily_notional_pct=Decimal("0.5"),
        daily_loss_pct=Decimal("0.5"),
        baseline_equity=Decimal("1000"),
        current_equity=Decimal("1000"),
        max_open_positions=4,
    )
    for key, value in kwargs.items():
        setattr(risk, key, value)
    return risk


def buy(symbol: str, quantity: str = "0.01", price: str = "100") -> OrderIntent:
    return OrderIntent(
        decision_id=f"d-{symbol}",
        symbol=symbol,
        side=Side.BUY,
        quantity=Decimal(quantity),
        ref_price=Decimal(price),
        reason="test",
        created_at=datetime(2026, 9, 16, tzinfo=timezone.utc),
    )


class CorrelationMathTests(unittest.TestCase):
    def test_identical_series_are_perfectly_correlated(self) -> None:
        series = [0.01, -0.02, 0.03, -0.01, 0.04, 0.02, -0.03, 0.01, 0.02, -0.01, 0.03]
        self.assertAlmostEqual(correlation(series, series), 1.0, places=6)

    def test_opposite_series_are_negatively_correlated(self) -> None:
        series = [0.01, -0.02, 0.03, -0.01, 0.04, 0.02, -0.03, 0.01, 0.02, -0.01, 0.03]
        self.assertAlmostEqual(
            correlation(series, [-value for value in series]), -1.0, places=6
        )

    def test_returns_and_computation_from_files(self) -> None:
        with tempfile.TemporaryDirectory() as name:
            tmp = Path(name)
            for stem, drift in (("BTCUSD", 0.02), ("ETHUSD", 0.02), ("SPY", -0.01)):
                price = 100.0
                rows = []
                for day in range(60):
                    price *= 1 + drift if day % 2 == 0 else 1 - drift / 2
                    rows.append(
                        json.dumps({"start": f"2026-01-{day:02d}", "close": str(price)})
                    )
                (tmp / f"{stem}_day.jsonl").write_text("\n".join(rows) + "\n")
            state = compute_state(tmp, ["BTC-USD", "ETH-USD", "SPY"], lookback=60)

        self.assertGreater(state.get("BTC-USD", "ETH-USD"), 0.7)
        self.assertLess(state.get("BTC-USD", "SPY"), 0.7)
        self.assertEqual(state.get("BTC-USD", "BTC-USD"), 1.0)

    def test_state_round_trips_through_disk(self) -> None:
        with tempfile.TemporaryDirectory() as name:
            state = CorrelationState(
                threshold=0.7,
                lookback=60,
                updated_at="now",
                pairs={"BTCUSD|ETHUSD": 0.91},
            )
            save_state(name, state)
            loaded = load_state(name)
        self.assertIsNotNone(loaded)
        self.assertAlmostEqual(loaded.get("BTC-USD", "ETH-USD"), 0.91)

    def test_a_corrupt_state_file_is_treated_as_missing(self) -> None:
        with tempfile.TemporaryDirectory() as name:
            (Path(name) / "correlations.json").write_text("{nope")
            self.assertIsNone(load_state(name))

    def test_unmeasured_pairs_are_reported_not_assumed_safe(self) -> None:
        state = CorrelationState(threshold=0.7, lookback=60, updated_at="now", pairs={})
        correlated, unknown = correlated_with("BTC-USD", ["ETH-USD"], state)
        self.assertEqual(correlated, [])
        self.assertEqual(unknown, ["ETH-USD"])


class ConcentrationGuardTests(unittest.TestCase):
    def test_a_correlated_cluster_hits_the_limit(self) -> None:
        state = CorrelationState(
            threshold=0.7,
            lookback=60,
            updated_at="now",
            pairs={"BTCUSD|ETHUSD": 0.91, "BTCUSD|SOLUSD": 0.88},
        )
        risk = guard(max_correlated_positions=2)
        risk._correlation_state = state  # type: ignore[attr-defined]
        snapshot = PortfolioSnapshot(
            open_positions=2,
            held={"ETH-USD": Decimal("1"), "SOL-USD": Decimal("1")},
        )
        decision = risk.evaluate(buy("BTC-USD"), snapshot)
        self.assertFalse(decision.allowed)
        self.assertIn("correlated_exposure", decision.reason)

    def test_an_uncorrelated_position_is_still_allowed(self) -> None:
        state = CorrelationState(
            threshold=0.7,
            lookback=60,
            updated_at="now",
            pairs={
                "BTCUSD|ETHUSD": 0.91,
                # The matrix is dense in practice: every pair the book could
                # hold against every candidate.
                "BTCUSD|SPY": 0.18,
                "ETHUSD|SPY": 0.22,
            },
        )
        risk = guard(max_correlated_positions=2)
        risk._correlation_state = state  # type: ignore[attr-defined]
        snapshot = PortfolioSnapshot(
            open_positions=2,
            held={"ETH-USD": Decimal("1"), "BTC-USD": Decimal("1")},
        )
        decision = risk.evaluate(buy("SPY"), snapshot)
        self.assertTrue(decision.allowed, decision.reason)

    def test_an_unknown_correlation_denies_rather_than_guesses(self) -> None:
        """No measurement, no extra room."""
        risk = guard(max_correlated_positions=2)
        risk._correlation_state = None  # type: ignore[attr-defined]
        snapshot = PortfolioSnapshot(
            open_positions=1, held={"ETH-USD": Decimal("1")}
        )
        decision = risk.evaluate(buy("SPY"), snapshot)
        self.assertFalse(decision.allowed)
        self.assertIn("correlation_unknown", decision.reason)

    def test_without_the_limit_the_old_behaviour_stands(self) -> None:
        risk = guard()  # max_correlated_positions defaults to None
        snapshot = PortfolioSnapshot(
            open_positions=3, held={"ETH-USD": Decimal("1")}
        )
        self.assertTrue(risk.evaluate(buy("SPY"), snapshot).allowed)


class ExecutionCostTests(unittest.TestCase):
    def test_trade_history_rows_are_parsed(self) -> None:
        payload = {
            "data": {
                "trades": [
                    {
                        "timestamp": "2026-09-16T10:00:00Z",
                        "symbol": "BTC",
                        "side": "buy",
                        "quantity": "0.001",
                        "price": "76010.00",
                        "realized_gain": "0",
                    },
                    {"symbol": "BTC", "price": "not-a-number"},
                ]
            }
        }
        trades = parse_trade_history(payload)
        self.assertEqual(len(trades), 1)
        self.assertEqual(trades[0]["price"], 76010.0)

    def test_a_fill_above_the_decision_price_costs_money(self) -> None:
        at = datetime(2026, 9, 16, 10, 0, tzinfo=timezone.utc)
        decisions = {
            "BTC-USD": [{"at": at, "price": 76000.0, "side": "buy"}],
        }
        trades = [
            {
                "timestamp": (at + timedelta(seconds=20)).isoformat(),
                "symbol": "BTCUSD",
                "side": "buy",
                "price": 76015.2,  # +2 bps
                "quantity": "0.001",
            }
        ]
        report = measure(trades, decisions, assumed_per_side_bps=2.0)
        self.assertEqual(report.fills, 1)
        self.assertAlmostEqual(report.measured_per_side_bps, 2.0, places=3)
        self.assertAlmostEqual(report.ratio, 1.0, places=3)

    def test_a_sell_below_the_decision_price_also_costs_money(self) -> None:
        at = datetime(2026, 9, 16, 10, 0, tzinfo=timezone.utc)
        decisions = {"BTC-USD": [{"at": at, "price": 76000.0, "side": "sell"}]}
        trades = [
            {
                "timestamp": (at + timedelta(minutes=1)).isoformat(),
                "symbol": "BTC-USD",
                "side": "sell",
                "price": 75984.8,  # sold 2bps below the decision price
                "quantity": "0.001",
            }
        ]
        report = measure(trades, decisions, assumed_per_side_bps=2.0)
        self.assertAlmostEqual(report.measured_per_side_bps, 2.0, places=3)

    def test_an_unrelated_fill_is_not_attributed(self) -> None:
        at = datetime(2026, 9, 16, 10, 0, tzinfo=timezone.utc)
        decisions = {"BTC-USD": [{"at": at, "price": 76000.0, "side": "buy"}]}
        trades = [
            {
                "timestamp": (at + timedelta(hours=5)).isoformat(),
                "symbol": "BTC-USD",
                "side": "buy",
                "price": 90000.0,
                "quantity": "0.001",
            }
        ]
        report = measure(trades, decisions, assumed_per_side_bps=2.0)
        self.assertEqual(report.fills, 0)
        self.assertIn("none matched", report.note)

    def test_decisions_come_from_the_journals_own_records(self) -> None:
        records = [
            {
                "event": "accepted",
                "symbol": "BTC-USD",
                "side": "buy",
                "ref_price": "76000.0",
                "intent": {"created_at": "2026-09-16T10:00:00+00:00"},
            },
            {"event": "rejected", "symbol": "BTC-USD"},
        ]
        prices = decision_prices(records)
        self.assertEqual(len(prices["BTC-USD"]), 1)

    def test_the_cost_model_switches_over_only_with_enough_fills(self) -> None:
        with tempfile.TemporaryDirectory() as name:
            save_report(
                name,
                CostReport(
                    fills=1,
                    measured_per_side_bps=12.0,
                    assumed_per_side_bps=2.0,
                    updated_at="now",
                ),
            )
            one_fill = cost_model_for(name)
            self.assertEqual(one_fill.per_side_bps, CostModel().per_side_bps)

            save_report(
                name,
                CostReport(
                    fills=40,
                    measured_per_side_bps=12.0,
                    assumed_per_side_bps=2.0,
                    updated_at="now",
                ),
            )
            measured = cost_model_for(name)
            self.assertAlmostEqual(float(measured.per_side_bps), 12.0, places=3)

    def test_an_absurd_measurement_is_not_trusted(self) -> None:
        with tempfile.TemporaryDirectory() as name:
            save_report(
                name,
                CostReport(
                    fills=50,
                    measured_per_side_bps=9000.0,
                    assumed_per_side_bps=2.0,
                    updated_at="now",
                ),
            )
            report = load_report(name)
            self.assertFalse(report.usable)
            self.assertEqual(
                cost_model_for(name).per_side_bps, CostModel().per_side_bps
            )


if __name__ == "__main__":
    unittest.main()
