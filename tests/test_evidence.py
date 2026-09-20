"""The evidence report: the artifact that ties order size to measured history."""

from __future__ import annotations

import json
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace

from agentic_trading.config import load_config
from agentic_trading.dashboard import _evidence_view
from agentic_trading.evidence import (
    build_report,
    effective_per_order_pct,
    effective_small_account_target,
    load_series,
    read_report,
    report_runtime_checks,
    shadow_forward_stats,
    write_report,
)
from agentic_trading.limits import Limits, save_limits

_DAY = datetime(2020, 1, 1, tzinfo=timezone.utc)


def _config(tmp: Path, *, symbols: list[str], days: int = 400):
    (tmp / "state").mkdir(parents=True, exist_ok=True)
    (tmp / "journal").mkdir(parents=True, exist_ok=True)
    bars = tmp / "bars"
    bars.mkdir(parents=True, exist_ok=True)
    for symbol in symbols:
        rows = []
        for index in range(days):
            price = 100.0 * (1.003**index)
            rows.append(
                json.dumps(
                    {
                        "symbol": symbol.replace("-", ""),
                        "start": (_DAY + timedelta(days=index)).isoformat(),
                        "open": f"{price:.4f}",
                        "high": f"{price:.4f}",
                        "low": f"{price:.4f}",
                        "close": f"{price:.4f}",
                        "volume": "10",
                    }
                )
            )
        (bars / f"{symbol.replace('-', '')}_day.jsonl").write_text(
            "\n".join(rows) + "\n"
        )
    path = tmp / "agentic.toml"
    path.write_text(
        "\n".join(
            [
                'mode = "shadow"',
                f"symbol_whitelist = {json.dumps(symbols)}",
                'max_order_pct = "0.01"',
                'daily_notional_pct = "0.04"',
                'daily_loss_pct = "0.03"',
                "max_open_positions = 2",
                "equity_refresh_ticks = 30",
                "equity_refresh_seconds = 60",
                'timezone = "local"',
                f'history_path = "{bars}"',
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
    return load_config(path)


class LoadSeriesTests(unittest.TestCase):
    def test_only_symbols_with_files_are_loaded(self) -> None:
        with tempfile.TemporaryDirectory() as name:
            tmp = Path(name)
            config = _config(tmp, symbols=["SPY", "BTC-USD"])
            (tmp / "bars" / "BTCUSD_day.jsonl").unlink()
            series = load_series(config)
        self.assertEqual(list(series), ["SPY"])

    def test_unknown_symbol_yields_nothing_rather_than_raising(self) -> None:
        with tempfile.TemporaryDirectory() as name:
            config = _config(Path(name), symbols=["SPY"])
            self.assertEqual(load_series(config, symbols=["NOPE"]), {})


class BuildReportTests(unittest.TestCase):
    def _report(self, tmp: Path) -> dict:
        config = _config(tmp, symbols=["SPY", "QQQ"], days=400)
        return build_report(
            config, folds=2, grid=(0.005, 0.01), max_positions=2, per_order_pct=0.01
        )

    def test_report_is_self_describing(self) -> None:
        with tempfile.TemporaryDirectory() as name:
            report = self._report(Path(name))
        self.assertIn("generated_at", report)
        self.assertEqual(report["drawdown_ceiling_pct"], 15.0)
        self.assertEqual(report["schema_version"], 4)
        self.assertEqual(report["starting_equity"], 50.0)
        self.assertEqual(report["series"]["bars"], 800)
        self.assertIn("production", report["configs"])
        self.assertIn("inverse_vol", report["configs"])
        self.assertEqual(report["cost_stress"]["multiplier"], 2.0)
        self.assertIn("return_pct", report["cost_stress"])
        self.assertTrue(report["notes"], "the report must state its own limits")
        self.assertEqual(report["strategy"], "fixture")
        self.assertEqual(report["base_sizing"], "flat")
        self.assertEqual(report["forward"]["completed_trades"], 0)

    def test_report_identity_must_match_the_current_strategy_book(self) -> None:
        with tempfile.TemporaryDirectory() as name:
            tmp = Path(name)
            config = _config(tmp, symbols=["SPY", "QQQ"], days=400)
            report = build_report(
                config,
                folds=2,
                grid=(0.01,),
                max_positions=2,
                per_order_pct=0.01,
            )

            self.assertTrue(
                report_runtime_checks(config, report)["model_check"]["current"]
            )
            mutations = {
                "strategy": lambda candidate: candidate.update(strategy="llm"),
                "sizing": lambda candidate: candidate.update(
                    base_sizing="proportional"
                ),
                "position limit": lambda candidate: candidate.update(max_positions=1),
                "universe": lambda candidate: candidate["series"].update(
                    symbols=["SPY"]
                ),
            }
            for label, mutate in mutations.items():
                with self.subTest(label=label):
                    candidate = json.loads(json.dumps(report))
                    mutate(candidate)
                    check = report_runtime_checks(config, candidate)["model_check"]
                    self.assertFalse(check["current"])

    def test_report_uses_current_account_equity_for_fixed_cost_scale(self) -> None:
        from agentic_trading import account

        with tempfile.TemporaryDirectory() as name:
            tmp = Path(name)
            config = _config(tmp, symbols=["SPY", "QQQ"], days=400)
            account.note_equity(config.state_dir, Decimal("250.25"))
            report = build_report(
                config,
                folds=2,
                grid=(0.01,),
                max_positions=2,
                per_order_pct=0.01,
            )
        self.assertEqual(report["starting_equity"], 250.25)

    def test_gate_size_never_exceeds_the_drawdown_ceiling(self) -> None:
        with tempfile.TemporaryDirectory() as name:
            report = self._report(Path(name))
        gate = report["gate_size"]
        if gate.get("per_order_pct") is None:
            self.assertIn("reason", gate)
        else:
            self.assertLessEqual(gate["max_drawdown_pct"], 15.0)

    def test_no_bars_is_an_error_not_an_empty_report(self) -> None:
        with tempfile.TemporaryDirectory() as name:
            tmp = Path(name)
            config = _config(tmp, symbols=["SPY"])
            (tmp / "bars" / "SPY_day.jsonl").unlink()
            with self.assertRaises(RuntimeError):
                build_report(config, folds=2, grid=(0.01,), max_positions=1)

    def test_report_uses_the_measured_fixed_execution_cost(self) -> None:
        from agentic_trading.execution import record_round_trip

        with tempfile.TemporaryDirectory() as name:
            tmp = Path(name)
            config = _config(tmp, symbols=["SPY", "QQQ"], days=400)
            record_round_trip(
                config.state_dir,
                symbol="SPY",
                buy_notional=5.00,
                sell_notional=4.90,
            )
            report = build_report(
                config,
                folds=2,
                grid=(0.01,),
                max_positions=2,
                per_order_pct=0.01,
            )

        self.assertEqual(report["costs"]["source"], "measured_round_trip_fixed_fee")
        self.assertAlmostEqual(
            report["costs"]["assumed_round_trip_fixed_usd"], 0.10, places=6
        )


class EffectiveSizeTests(unittest.TestCase):
    def test_the_ladder_size_is_used_when_it_exists(self) -> None:
        with tempfile.TemporaryDirectory() as name:
            tmp = Path(name)
            config = _config(tmp, symbols=["SPY", "QQQ"], days=60)
            save_limits(
                tmp / "state",
                Limits(
                    max_order_pct="0.006",
                    daily_notional_pct="0.024",
                    reason="test",
                    updated_at="now",
                ),
            )
            # per_order_pct=None means "price the book that is actually running".
            report = build_report(config, folds=2, grid=(0.01,), max_positions=1)
        self.assertEqual(report["configs"]["production"]["per_order_pct"], 0.006)

    def test_enabled_five_dollar_floor_is_the_tested_production_size(self) -> None:
        with tempfile.TemporaryDirectory() as name:
            tmp = Path(name)
            _config(tmp, symbols=["SPY", "QQQ"], days=400)
            path = tmp / "agentic.toml"
            path.write_text(
                path.read_text(encoding="utf-8")
                + 'small_account_target_notional = "5.00"\n'
                + 'small_account_max_order_pct = "0.11"\n'
                + 'small_account_max_daily_pct = "0.11"\n',
                encoding="utf-8",
            )
            config = load_config(path)
            report = build_report(
                config,
                folds=2,
                grid=(0.005, 0.01),
                max_positions=2,
            )
        production_size = report["configs"]["production"]["per_order_pct"]
        frontier = [row["per_order_pct"] for row in report["size_frontier"]]
        self.assertAlmostEqual(production_size, 0.102, places=9)
        self.assertIn(production_size, frontier)
        self.assertEqual(report["sizing"], "small_account_floor")
        self.assertFalse(report["small_account_floor"]["proportional"])
        self.assertAlmostEqual(
            report["small_account_floor"]["sized_notional"], 5.10, places=9
        )
        production_point = next(
            row
            for row in report["size_frontier"]
            if row["per_order_pct"] == production_size
        )
        self.assertEqual(production_point["sizing"], "small_account_floor")
        self.assertTrue(
            report_runtime_checks(config, report)["sizing_check"]["current"]
        )

    def test_percentage_override_cannot_authorize_a_fixed_dollar_floor(self) -> None:
        with tempfile.TemporaryDirectory() as name:
            tmp = Path(name)
            _config(tmp, symbols=["SPY", "QQQ"], days=400)
            path = tmp / "agentic.toml"
            path.write_text(
                path.read_text(encoding="utf-8")
                + 'small_account_target_notional = "5.00"\n'
                + 'small_account_max_order_pct = "0.11"\n'
                + 'small_account_max_daily_pct = "0.11"\n',
                encoding="utf-8",
            )
            config = load_config(path)
            report = build_report(
                config,
                per_order_pct=0.11,
                folds=2,
                grid=(0.11,),
                max_positions=2,
            )

        self.assertNotIn("small_account_floor", report)
        check = report_runtime_checks(config, report)["sizing_check"]
        self.assertFalse(check["current"])
        self.assertFalse(check["floor_model_current"])

    def test_measured_fixed_cost_can_raise_the_useful_target(self) -> None:
        from agentic_trading.execution import record_round_trip

        with tempfile.TemporaryDirectory() as name:
            tmp = Path(name)
            config = _config(tmp, symbols=["SPY"])
            record_round_trip(
                config.state_dir,
                symbol="SPY",
                buy_notional=10.0,
                sell_notional=9.8,
            )
            target = effective_small_account_target(config)
        # A $0.20 round trip capped at 2% needs a $10 order, even though the
        # configured research floor is only $5.
        self.assertAlmostEqual(float(target), 10.0, places=6)


class RoundTripTests(unittest.TestCase):
    def test_write_then_read_and_no_temp_file_left_behind(self) -> None:
        with tempfile.TemporaryDirectory() as name:
            tmp = Path(name)
            config = _config(tmp, symbols=["SPY"])
            path = write_report(config, {"generated_at": "2026-01-01T00:00:00+00:00"})
            stored = read_report(config)
            leftovers = [p for p in path.parent.iterdir() if p.suffix == ".tmp"]
        assert stored is not None
        self.assertEqual(stored["generated_at"], "2026-01-01T00:00:00+00:00")
        self.assertEqual(leftovers, [])

    def test_unreadable_report_reads_as_none(self) -> None:
        with tempfile.TemporaryDirectory() as name:
            tmp = Path(name)
            config = _config(tmp, symbols=["SPY"])
            (tmp / "state" / "strategy_evidence.json").write_text("{not json")
            self.assertIsNone(read_report(config))


class ForwardShadowEvidenceTests(unittest.TestCase):
    def test_only_completed_shadow_round_trips_count(self) -> None:
        from agentic_trading.journal import DecisionJournal

        with tempfile.TemporaryDirectory() as name:
            tmp = Path(name)
            config = _config(tmp, symbols=["SPY"])
            journal = DecisionJournal(Path(config.journal_dir))
            journal.append(
                {
                    "event": "accepted",
                    "mode": "shadow",
                    "side": "buy",
                    "symbol": "SPY",
                    "quantity": "1",
                    "ref_price": "100",
                    "at": "2026-01-01T00:00:00+00:00",
                    "intent": {
                        "symbol": "SPY",
                        "side": "buy",
                        "quantity": "1",
                        "ref_price": "100",
                    },
                }
            )
            journal.append(
                {
                    "event": "accepted",
                    "mode": "shadow",
                    "side": "sell",
                    "symbol": "SPY",
                    "quantity": "1",
                    "ref_price": "110",
                    "at": "2026-01-10T00:00:00+00:00",
                    "intent": {
                        "symbol": "SPY",
                        "side": "sell",
                        "quantity": "1",
                        "ref_price": "110",
                    },
                }
            )
            # A live approval is not a fill and cannot improve the forward record.
            journal.append(
                {
                    "event": "accepted",
                    "mode": "live",
                    "side": "sell",
                    "symbol": "SPY",
                    "quantity": "1",
                    "ref_price": "999",
                    "intent": {
                        "symbol": "SPY",
                        "side": "sell",
                        "quantity": "1",
                        "ref_price": "999",
                    },
                }
            )

            stats = shadow_forward_stats(
                config, now=datetime(2026, 1, 11, tzinfo=timezone.utc)
            )

        self.assertEqual(stats["completed_trades"], 1)
        self.assertEqual(stats["wins"], 1)
        self.assertEqual(stats["observed_days"], 10.0)
        self.assertAlmostEqual(float(stats["realized_pnl"]), 9.958, places=3)


class EvidenceViewTests(unittest.TestCase):
    def test_missing_report_is_none_not_an_empty_shell(self) -> None:
        self.assertIsNone(_evidence_view(None))

    def test_view_keeps_the_numbers_that_justify_size(self) -> None:
        view = _evidence_view(
            {
                "generated_at": "2026-01-01T00:00:00+00:00",
                "drawdown_ceiling_pct": 15.0,
                "series": {"symbols": ["SPY"], "bars": 10},
                "folds": 6,
                "configs": {
                    "production": {
                        "per_order_pct": 0.01,
                        "trades": 495,
                        "expectancy_bps": 856,
                        "max_drawdown_pct": 13.96,
                        "final_equity": 74.22,
                        "bootstrap_p_value": 0.0005,
                        "eligible": True,
                        "folds": [{"index": 0}],
                    }
                },
                "gate_size": {"per_order_pct": 0.01, "max_drawdown_pct": 13.96},
            }
        )
        assert view is not None
        self.assertEqual(view["production"]["max_drawdown_pct"], 13.96)
        self.assertNotIn("folds", view["production"])
        self.assertEqual(view["symbols"], ["SPY"])


if __name__ == "__main__":
    unittest.main()


class StalenessTests(unittest.TestCase):
    """The gate refuses a stale report; something has to notice that."""

    def _at(self, days_ago: float, *, config=None) -> dict:
        from datetime import datetime, timedelta, timezone

        report = {
            "schema_version": 4,
            "generated_at": (
                datetime.now(timezone.utc) - timedelta(days=days_ago)
            ).isoformat(),
            "configs": {"production": {"per_order_pct": 0.01}},
        }
        if config is not None:
            report.update(
                {
                    "strategy": config.strategy,
                    "base_sizing": config.sizing,
                    "max_positions": config.max_open_positions,
                    "series": {"symbols": sorted(config.effective_whitelist)},
                }
            )
        return report

    def test_age_is_measured_from_the_timestamp(self) -> None:
        from agentic_trading.evidence import report_age_days

        self.assertAlmostEqual(report_age_days(self._at(3)), 3.0, places=2)

    def test_missing_unreadable_or_old_reports_are_stale(self) -> None:
        from agentic_trading.evidence import is_stale

        self.assertTrue(is_stale(None, max_age_days=7))
        self.assertTrue(is_stale({"generated_at": "not-a-date"}, max_age_days=7))
        self.assertTrue(is_stale(self._at(30), max_age_days=7))
        self.assertFalse(is_stale(self._at(1), max_age_days=7))

    def test_a_fresh_report_is_left_alone(self) -> None:
        from unittest import mock

        from agentic_trading import evidence as module

        with tempfile.TemporaryDirectory() as name:
            tmp = Path(name)
            config = _config(tmp, symbols=["SPY"])
            write_report(config, self._at(1, config=config))
            with mock.patch.object(module, "build_report") as builder:
                out = module.refresh_if_stale(config, max_age_days=7)
        self.assertIsNone(out)
        builder.assert_not_called()

    def test_a_stale_report_is_rebuilt_and_written(self) -> None:
        from unittest import mock

        from agentic_trading import evidence as module

        with tempfile.TemporaryDirectory() as name:
            tmp = Path(name)
            config = _config(tmp, symbols=["SPY"])
            write_report(config, {"generated_at": self._at(30)["generated_at"]})
            with mock.patch.object(
                module, "build_report", return_value={"generated_at": "fresh"}
            ) as builder:
                out = module.refresh_if_stale(config, max_age_days=7)
                stored = read_report(config)
        builder.assert_called_once()
        assert out is not None
        self.assertEqual(stored["generated_at"], "fresh")
        # The rebuild records why it ran, so a silent weekly rewrite is auditable.
        self.assertGreaterEqual(stored["age_at_build_days"], 29)
        self.assertEqual(stored["refresh_max_age_days"], 7)

    def test_a_missing_report_is_rebuilt(self) -> None:
        from unittest import mock

        from agentic_trading import evidence as module

        with tempfile.TemporaryDirectory() as name:
            config = _config(Path(name), symbols=["SPY"])
            with mock.patch.object(
                module, "build_report", return_value={"generated_at": "fresh"}
            ) as builder:
                module.refresh_if_stale(config, max_age_days=7)
        builder.assert_called_once()

    def test_a_report_below_the_new_floor_size_is_rebuilt(self) -> None:
        from unittest import mock

        from agentic_trading import evidence as module

        with tempfile.TemporaryDirectory() as name:
            tmp = Path(name)
            _config(tmp, symbols=["SPY"])
            path = tmp / "agentic.toml"
            path.write_text(
                path.read_text(encoding="utf-8")
                + 'small_account_max_order_pct = "0.11"\n'
                + 'small_account_max_daily_pct = "0.11"\n',
                encoding="utf-8",
            )
            config = load_config(path)
            write_report(config, self._at(1, config=config))
            fresh = {
                "schema_version": 4,
                "generated_at": datetime.now(timezone.utc).isoformat(),
                "configs": {
                    "production": {"per_order_pct": effective_per_order_pct(config)}
                },
            }
            with mock.patch.object(
                module, "build_report", return_value=fresh
            ) as builder:
                rebuilt = module.refresh_if_stale(config, max_age_days=7)
        builder.assert_called_once()
        assert rebuilt is not None
        self.assertTrue(rebuilt["sizing_refresh"])

    def test_a_changed_strategy_book_is_rebuilt(self) -> None:
        from unittest import mock

        from agentic_trading import evidence as module

        with tempfile.TemporaryDirectory() as name:
            tmp = Path(name)
            config = _config(tmp, symbols=["SPY"])
            existing = self._at(1, config=config)
            existing["base_sizing"] = "proportional"
            write_report(config, existing)
            with mock.patch.object(
                module,
                "build_report",
                return_value={"generated_at": "fresh"},
            ) as builder:
                rebuilt = module.refresh_if_stale(config, max_age_days=7)

        builder.assert_called_once()
        assert rebuilt is not None
        self.assertTrue(rebuilt["model_refresh"])


class _FakeLoop:
    """Just enough of the loop for the worker paths: equity and the roster."""

    def __init__(self, equity: str = "50") -> None:
        self.guard = SimpleNamespace(current_equity=Decimal(equity))
        self.agents: list[tuple[str, bool, dict]] = []

    def note_agent(self, name, *, ok, detail=None, error="") -> None:
        self.agents.append((name, ok, dict(detail or {})))


class DaemonRefreshTests(unittest.TestCase):
    """The daemon has to refresh it without being asked."""

    def _config(self, tmp: Path, *, days: float):
        _config(tmp, symbols=["SPY"])
        path = tmp / "agentic.toml"
        path.write_text(
            path.read_text(encoding="utf-8") + f"evidence_refresh_days = {days}\n",
            encoding="utf-8",
        )
        return load_config(path)

    def test_disabled_refresh_does_nothing(self) -> None:
        from unittest import mock

        from agentic_trading import runtime as module

        with tempfile.TemporaryDirectory() as name:
            config = self._config(Path(name), days=0)
            loop = _FakeLoop()
            journal = mock.Mock()
            with mock.patch("agentic_trading.evidence.refresh_if_stale") as refresh:
                module._refresh_evidence(config, loop, journal)
        refresh.assert_not_called()
        journal.append.assert_not_called()
        self.assertEqual(loop.agents, [], "a disabled refresh is not an agent run")

    def test_a_refresh_is_journaled_with_its_numbers(self) -> None:
        from unittest import mock

        from agentic_trading import runtime as module

        with tempfile.TemporaryDirectory() as name:
            config = self._config(Path(name), days=7)
            loop = _FakeLoop()
            journal = mock.Mock()
            report = {
                "configs": {"production": {"per_order_pct": 0.01, "trades": 495}},
                "gate_size": {"per_order_pct": 0.01},
                "series": {"symbols": ["SPY"], "bars": 100},
                "age_at_build_days": 12.0,
            }
            with mock.patch(
                "agentic_trading.evidence.refresh_if_stale", return_value=report
            ):
                module._refresh_evidence(config, loop, journal)
        events = [call.args[0] for call in journal.append.call_args_list]
        self.assertEqual(events[0]["event"], "evidence_refreshed")
        self.assertEqual(events[0]["trades"], 495)
        self.assertEqual(events[0]["age_at_build_days"], 12.0)
        # The research agent's health comes from this run, not from a label.
        self.assertEqual(loop.agents[0][0], "research")
        self.assertTrue(loop.agents[0][1])
        self.assertEqual(loop.agents[0][2]["trades"], 495)

    def test_a_failure_is_journaled_and_not_retried_immediately(self) -> None:
        from unittest import mock

        from agentic_trading import runtime as module

        with tempfile.TemporaryDirectory() as name:
            config = self._config(Path(name), days=7)
            loop = _FakeLoop()
            journal = mock.Mock()
            with mock.patch(
                "agentic_trading.evidence.refresh_if_stale",
                side_effect=RuntimeError("no bars"),
            ) as refresh:
                module._refresh_evidence(config, loop, journal)
                module._refresh_evidence(config, loop, journal)
        self.assertEqual(refresh.call_count, 1, "a failure must not spin every cycle")
        events = [call.args[0] for call in journal.append.call_args_list]
        self.assertEqual(events[0]["event"], "evidence_refresh_failed")
        self.assertIn("no bars", events[0]["error"])


class SizeFrontierTests(unittest.TestCase):
    """The report must state what sizing up costs, not just what it earns."""

    def test_the_report_carries_a_measured_size_frontier(self) -> None:
        with tempfile.TemporaryDirectory() as name:
            tmp = Path(name)
            config = _config(tmp, symbols=["SPY", "QQQ"], days=400)
            report = build_report(
                config, folds=2, grid=(0.01, 0.02), max_positions=2, per_order_pct=0.01
            )
        frontier = report.get("size_frontier")
        self.assertTrue(frontier, "the console needs the measured cost of sizing up")
        self.assertEqual([row["per_order_pct"] for row in frontier], [0.01, 0.02])
        for row in frontier:
            self.assertIn("max_drawdown_pct", row)
            self.assertIn("inside_ceiling", row)
        # Drawdown scales with size, which is the whole reason the frontier exists.
        self.assertLessEqual(
            frontier[0]["max_drawdown_pct"], frontier[-1]["max_drawdown_pct"]
        )
