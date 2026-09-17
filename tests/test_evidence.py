"""The evidence report: the artifact that ties order size to measured history."""

from __future__ import annotations

import json
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from agentic_trading.config import load_config
from agentic_trading.dashboard import _evidence_view
from agentic_trading.evidence import (
    build_report,
    load_series,
    read_report,
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
        self.assertEqual(report["series"]["bars"], 800)
        self.assertIn("production", report["configs"])
        self.assertIn("inverse_vol", report["configs"])
        self.assertTrue(report["notes"], "the report must state its own limits")

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


class RoundTripTests(unittest.TestCase):
    def test_write_then_read_and_no_temp_file_left_behind(self) -> None:
        with tempfile.TemporaryDirectory() as name:
            tmp = Path(name)
            config = _config(tmp, symbols=["SPY"])
            path = write_report(
                config, {"generated_at": "2026-01-01T00:00:00+00:00"}
            )
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
