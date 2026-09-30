"""Momentum rotation: the rule, its strategy, its gate and its shadow trial."""

from __future__ import annotations

import json
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path

from agentic_trading.history import Bar
from agentic_trading.promotion import PromotionPolicy, assess_walkforward
from agentic_trading.walkforward import (
    rank_rotation,
    rotation_anchor,
    rule_for_strategy,
    targets_as_of,
)
from tests import test_evolution_promotion as gate_fixtures

START = datetime(2026, 1, 1, tzinfo=timezone.utc)


def _series(symbol: str, *, days: int = 260, drift: float = 0.0) -> list[Bar]:
    """A daily series whose close compounds by ``drift`` per day."""
    bars = []
    price = 100.0
    for day in range(days):
        price *= 1 + drift
        close = Decimal(str(round(price, 6)))
        bars.append(
            Bar(
                symbol=symbol,
                start=START + timedelta(days=day),
                open=close,
                high=close,
                low=close,
                close=close,
            )
        )
    return bars


def _book(**drifts: float) -> dict[str, list[Bar]]:
    return {symbol: _series(symbol, drift=drift) for symbol, drift in drifts.items()}


WHEN = START + timedelta(days=250)


class RotationRuleTests(unittest.TestCase):
    def test_each_book_holds_its_strongest_positive_names(self) -> None:
        series = _book(
            SPY=0.001,
            QQQ=0.002,
            NVDA=0.004,
            AAPL=0.003,
            TSLA=-0.002,
            **{"BTC-USD": 0.002, "ETH-USD": 0.003, "SOL-USD": 0.001, "LTC-USD": -0.001},
        )
        targets = targets_as_of(series, WHEN, rule="rotation")
        self.assertEqual(set(targets), {"NVDA", "AAPL", "QQQ", "ETH-USD", "BTC-USD"})
        self.assertTrue(all(weight == 1.0 for weight in targets.values()))

    def test_a_negative_return_is_never_held_even_with_empty_slots(self) -> None:
        series = _book(SPY=0.001, TSLA=-0.002, **{"BTC-USD": 0.001})
        self.assertNotIn("TSLA", targets_as_of(series, WHEN, rule="rotation"))

    def test_a_book_below_its_regime_average_holds_cash(self) -> None:
        # SPY falling puts the equity book in cash; crypto is unaffected.
        series = _book(SPY=-0.001, NVDA=0.004, **{"BTC-USD": 0.002})
        targets = targets_as_of(series, WHEN, rule="rotation")
        self.assertNotIn("NVDA", targets)
        self.assertIn("BTC-USD", targets)
        nvda = next(
            row for row in rank_rotation(series, WHEN) if row["symbol"] == "NVDA"
        )
        self.assertIn("SPY is not above its 200-day average", nvda["reason"])

    def test_a_missing_regime_symbol_fails_closed(self) -> None:
        series = _book(NVDA=0.004, **{"ETH-USD": 0.003})
        self.assertEqual(targets_as_of(series, WHEN, rule="rotation"), {})

    def test_the_book_only_changes_on_the_weekly_boundary(self) -> None:
        # A leader change mid-week must wait for Monday.
        series = _book(
            SPY=0.001, QQQ=0.002, AAPL=0.003, MSFT=0.0025, **{"BTC-USD": 0.001}
        )
        monday = rotation_anchor(WHEN)
        crash = [
            Bar(
                symbol="AAPL",
                start=bar.start,
                open=bar.close,
                high=bar.close,
                low=bar.close,
                close=bar.close / 3 if bar.start >= monday else bar.close,
            )
            for bar in series["AAPL"]
        ]
        series["AAPL"] = crash
        friday = monday + timedelta(days=4)
        self.assertIn("AAPL", targets_as_of(series, friday, rule="rotation"))
        self.assertNotIn(
            "AAPL", targets_as_of(series, monday + timedelta(days=7), rule="rotation")
        )

    def test_the_anchor_is_monday_midnight_utc(self) -> None:
        wednesday = datetime(2026, 9, 23, 15, 30, tzinfo=timezone.utc)
        self.assertEqual(
            rotation_anchor(wednesday), datetime(2026, 9, 21, tzinfo=timezone.utc)
        )

    def test_strategies_map_to_the_rule_they_trade(self) -> None:
        self.assertEqual(rule_for_strategy("momentum_rotation"), "rotation")
        self.assertEqual(rule_for_strategy("trend_crypto"), "trend")


class RotationStrategyTests(unittest.TestCase):
    def test_the_strategy_trades_the_rule_the_evidence_grades(self) -> None:
        from agentic_trading.strategies.momentum_rotation import (
            MomentumRotationStrategy,
        )

        series = _book(SPY=0.001, QQQ=0.002, NVDA=0.004, **{"BTC-USD": 0.002})
        with tempfile.TemporaryDirectory() as name:
            bar_dir = Path(name)
            for symbol, bars in series.items():
                path = bar_dir / f"{symbol.replace('-', '')}_day.jsonl"
                path.write_text(
                    "".join(json.dumps(bar.to_record()) + "\n" for bar in bars),
                    encoding="utf-8",
                )
            strategy = MomentumRotationStrategy(
                bar_dir=bar_dir, symbols=list(series), max_positions=5
            )
            weights = strategy.target_weights(as_of=WHEN)
        keyed = {symbol.replace("-", ""): bars for symbol, bars in series.items()}
        self.assertEqual(set(weights), set(targets_as_of(keyed, WHEN, rule="rotation")))
        self.assertEqual(set(weights), {"NVDA", "QQQ", "SPY", "BTCUSD"})


    def test_new_bars_reach_a_running_strategy(self) -> None:
        """A sync must change what the rotation ranks, or it holds forever."""
        from agentic_trading.strategies.momentum_rotation import (
            MomentumRotationStrategy,
        )

        def write(bar_dir: Path, series: dict[str, list[Bar]]) -> None:
            for symbol, bars in series.items():
                path = bar_dir / f"{symbol.replace('-', '')}_day.jsonl"
                path.write_text(
                    "".join(json.dumps(bar.to_record()) + "\n" for bar in bars),
                    encoding="utf-8",
                )

        old = _book(SPY=0.001, AAPL=0.003, MSFT=0.001, **{"BTC-USD": 0.001})
        with tempfile.TemporaryDirectory() as name:
            bar_dir = Path(name)
            write(bar_dir, old)
            strategy = MomentumRotationStrategy(
                bar_dir=bar_dir, symbols=list(old), max_positions=5
            )
            later = WHEN + timedelta(days=21)
            self.assertIn("AAPL", strategy.target_weights(as_of=later))
            # Three more weeks of bars: AAPL collapses.
            extended = {
                symbol: bars
                + [
                    Bar(
                        symbol=symbol,
                        start=bars[-1].start + timedelta(days=step),
                        open=bars[-1].close,
                        high=bars[-1].close,
                        low=bars[-1].close,
                        close=bars[-1].close / 2 if symbol == "AAPL" else bars[-1].close,
                    )
                    for step in range(1, 22)
                ]
                for symbol, bars in old.items()
            }
            write(bar_dir, extended)
            self.assertEqual(strategy.reload_history(), 4)
            self.assertNotIn("AAPL", strategy.target_weights(as_of=later))
            self.assertEqual(strategy.reload_history(), 0)


class RotationGateTests(unittest.TestCase):
    def test_a_selected_rule_pays_for_every_hypothesis_examined(self) -> None:
        fixture = gate_fixtures.WalkForwardGateTests()
        report = fixture._report(hypotheses=40, production={"bootstrap_p_value": 0.01})
        assessment = assess_walkforward(
            report, PromotionPolicy(), live_per_order_pct=0.01
        )
        self.assertFalse(assessment.eligible)
        self.assertEqual(assessment.evidence["tested_hypotheses"], 40)
        self.assertTrue(
            any("0.05 / 40 hypotheses" in reason for reason in assessment.reasons),
            assessment.reasons,
        )

    def test_the_rotation_requires_forward_shadow_evidence(self) -> None:
        from agentic_trading.promotion import policy_from_config

        class _Config:
            strategy = "momentum_rotation"

        policy = policy_from_config(_Config())
        self.assertEqual(policy.min_forward_trades, 30)


class ShadowFullSizeTests(unittest.TestCase):
    def _loop(self, tmp: Path, *, mode: str, full_size: bool):
        from agentic_trading.broker import Broker
        from agentic_trading.config import load_config
        from agentic_trading.limits import Limits, save_limits
        from agentic_trading.runtime import _Loop
        from agentic_trading.strategies.fixture import FixtureStrategy
        from tests.fakes import FakeMcpClient
        from tests.test_runtime_daemon import _write_config, load_tools

        extra = [f"shadow_full_size = {'true' if full_size else 'false'}"]
        config = load_config(
            _write_config(tmp, mode=mode, max_order_pct="0.19", extra=extra)
        )
        (tmp / "state").mkdir(parents=True, exist_ok=True)
        # A confidence-0 ladder: a quarter-percent per order.
        save_limits(
            config.state_dir,
            Limits(
                max_order_pct="0.0025",
                daily_notional_pct="0.01",
                reason="confidence_flat",
                updated_at=datetime.now(timezone.utc).isoformat(),
            ),
        )
        broker = Broker(FakeMcpClient(load_tools()), load_tools())
        return _Loop(config, broker, FixtureStrategy())

    def test_a_shadow_trial_sizes_at_the_operator_ceiling(self) -> None:
        with tempfile.TemporaryDirectory() as name:
            loop = self._loop(Path(name), mode="shadow", full_size=True)
        self.assertEqual(loop.guard.max_order_pct, Decimal("0.19"))
        self.assertEqual(loop.guard.daily_notional_pct, Decimal("0.20"))

    def test_without_the_switch_the_ladder_still_binds(self) -> None:
        with tempfile.TemporaryDirectory() as name:
            loop = self._loop(Path(name), mode="shadow", full_size=False)
        self.assertEqual(loop.guard.max_order_pct, Decimal("0.0025"))

    def test_the_switch_never_reaches_live_orders(self) -> None:
        with tempfile.TemporaryDirectory() as name:
            loop = self._loop(Path(name), mode="live", full_size=True)
        self.assertEqual(loop.guard.max_order_pct, Decimal("0.0025"))


class TrialScoreTests(unittest.TestCase):
    def test_the_trial_scores_the_book_against_buy_and_hold(self) -> None:
        from agentic_trading import trial
        from agentic_trading.config import load_config
        from tests.test_runtime_daemon import _write_config

        with tempfile.TemporaryDirectory() as name:
            tmp = Path(name)
            bars = tmp / "bars"
            bars.mkdir()
            config = load_config(_write_config(tmp, extra=[f'history_path = "{bars}"']))
            started = datetime(2026, 9, 1, tzinfo=timezone.utc)

            def write(symbol: str, closes: list[float]) -> None:
                rows = [
                    {
                        "symbol": symbol,
                        "start": (started + timedelta(days=offset - 1)).isoformat(),
                        "open": str(close),
                        "high": str(close),
                        "low": str(close),
                        "close": str(close),
                    }
                    for offset, close in enumerate(closes)
                ]
                (bars / f"{symbol}_day.jsonl").write_text(
                    "".join(json.dumps(row) + "\n" for row in rows), encoding="utf-8"
                )

            write("QQQ", [100, 100, 110])  # +10%
            write("BTCUSD", [100, 100, 100])  # flat
            write("NVDA", [10, 10, 20])  # doubles
            trial._path(config).parent.mkdir(parents=True, exist_ok=True)
            trial.start_trial(config, starting_equity=Decimal("50"), now=started)
            journal = tmp / "journal"
            journal.mkdir()
            records = [
                # Before the trial: ignored, as is its later sale.
                {
                    "event": "accepted",
                    "mode": "shadow",
                    "at": "2026-08-30T00:00:00+00:00",
                    "intent": {
                        "symbol": "BTC-USD",
                        "side": "buy",
                        "quantity": "1",
                        "ref_price": "5",
                    },
                },
                {
                    "event": "accepted",
                    "mode": "shadow",
                    "at": "2026-09-01T12:00:00+00:00",
                    "intent": {
                        "symbol": "NVDA",
                        "side": "buy",
                        "quantity": "1",
                        "ref_price": "10",
                    },
                },
                {
                    "event": "accepted",
                    "mode": "shadow",
                    "at": "2026-09-01T13:00:00+00:00",
                    "intent": {
                        "symbol": "BTC-USD",
                        "side": "sell",
                        "quantity": "1",
                        "ref_price": "5",
                    },
                },
                # Live acceptances are approvals, not fills.
                {
                    "event": "accepted",
                    "mode": "live",
                    "at": "2026-09-01T14:00:00+00:00",
                    "intent": {
                        "symbol": "NVDA",
                        "side": "buy",
                        "quantity": "1",
                        "ref_price": "10",
                    },
                },
            ]
            (journal / "2026-09-01.jsonl").write_text(
                "".join(json.dumps(record) + "\n" for record in records),
                encoding="utf-8",
            )
            early = trial.score_trial(config, now=started + timedelta(days=2))
            late = trial.score_trial(config, now=started + timedelta(days=31))
        # 50 - 10.003 cash + 1 NVDA at 20 = 59.997 -> +20%; benchmark 0.6*10% = +6%.
        self.assertAlmostEqual(early["book_return_pct"], 19.99, places=2)
        self.assertAlmostEqual(early["benchmark_return_pct"], 6.0, places=2)
        self.assertEqual((early["entries"], early["exits"]), (1, 0))
        self.assertEqual(early["verdict"], "running")
        self.assertEqual(late["verdict"], "keep")
        self.assertEqual(early["lowest_cash"], 40.0)
        self.assertNotIn("WARNING", trial.format_trial(early))


if __name__ == "__main__":
    unittest.main()


class ShadowPaperExitTests(unittest.TestCase):
    """A paper position's exit must not trip the kill switch.

    The broker previews every order against the *real* account; a position
    that only exists on paper is refused there ("you can only sell up to 0").
    """

    def _run(self, mode: str, error: str):
        import os
        from unittest import mock

        from agentic_trading.broker import Broker
        from agentic_trading.config import load_config
        from agentic_trading.types import OrderIntent, Side
        from tests.fakes import FakeMcpClient
        from tests.test_runtime_daemon import (
            _StubFeed,
            _quote,
            _records,
            _run,
            _write_config,
            load_tools,
        )

        class _BuyThenSell:
            def __init__(self) -> None:
                self.calls = 0

            def on_quote(self, quote: dict) -> list[OrderIntent]:
                self.calls += 1
                side = Side.BUY if self.calls == 1 else Side.SELL
                return [
                    OrderIntent(
                        decision_id=f"paper-{self.calls}",
                        symbol=quote["symbol"],
                        side=side,
                        quantity=Decimal("0.01"),
                        ref_price=quote["ask"],
                        reason="test",
                        created_at=datetime(2026, 9, 16, 14, 0, tzinfo=timezone.utc),
                    )
                ]

        tools = load_tools()
        client = FakeMcpClient(tools)
        original = client.call_tool

        def call(name: str, arguments: dict) -> dict:
            if name == "review_equity_order" and arguments.get("side") == "sell":
                raise RuntimeError(error)
            return original(name, arguments)

        client.call_tool = call  # type: ignore[assignment]
        broker = Broker(client, tools)
        with tempfile.TemporaryDirectory() as name:
            tmp = Path(name)
            config = load_config(_write_config(tmp, mode=mode))
            with mock.patch.dict(os.environ, {"AGENTIC_ALLOW_LIVE": ""}):
                _run(
                    config,
                    broker,
                    _BuyThenSell(),
                    _StubFeed([_quote(), _quote(second=5)]),
                )
            return _records(config)

    def test_a_refused_paper_exit_is_recorded_not_counted_as_an_error(self) -> None:
        records = self._run(
            "shadow", "isError: {'rh_error_category': 'invalid_request'} sell up to 0"
        )
        exits = [
            r
            for r in records
            if r.get("event") == "accepted" and r.get("side") == "sell"
        ]
        self.assertEqual(len(exits), 1)
        self.assertTrue(exits[0]["review"]["shadow_only_position"])
        events = [r.get("event") for r in records]
        self.assertNotIn("review_failed", events)
        self.assertNotIn("error_streak", events)

    def test_a_transport_failure_still_counts(self) -> None:
        records = self._run("shadow", "ConnectError: name or service not known")
        self.assertIn("review_failed", [r.get("event") for r in records])


class PerClassCostTests(unittest.TestCase):
    def test_the_simulator_charges_crypto_costs_only_to_crypto(self) -> None:
        from agentic_trading.backtest import CostModel
        from agentic_trading.walkforward import simulate

        series = _book(SPY=0.001, QQQ=0.002, **{"BTC-USD": 0.002})
        free = CostModel(Decimal("0"), Decimal("0"), Decimal("0"))
        crypto_fee = CostModel(
            Decimal("0"),
            Decimal("0"),
            Decimal("0"),
            crypto=CostModel(Decimal("0"), Decimal("0"), Decimal("0.5")),
        )

        def pnl_by_symbol(costs: CostModel) -> dict[str, float]:
            trades, _ = simulate(
                series,
                start=START + timedelta(days=220),
                end=START + timedelta(days=259),
                costs=costs,
                per_order_pct=0.19,
                proportional=True,
                rule="rotation",
            )
            out: dict[str, float] = {}
            for trade in trades:
                out[trade["symbol"]] = out.get(trade["symbol"], 0.0) + trade["pnl"]
            return out

        gross, charged = pnl_by_symbol(free), pnl_by_symbol(crypto_fee)
        self.assertEqual(set(gross), {"SPY", "QQQ", "BTC-USD"})
        self.assertAlmostEqual(charged["SPY"], gross["SPY"], places=9)
        self.assertAlmostEqual(charged["QQQ"], gross["QQQ"], places=9)
        self.assertLess(charged["BTC-USD"], gross["BTC-USD"] - 0.9)
