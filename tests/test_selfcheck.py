"""The back-check agent must notice broken backends, data and analysis paths."""

from __future__ import annotations

import json
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

from agentic_trading.config import load_config
from agentic_trading.selfcheck import (
    FAIL,
    OK,
    WARN,
    check_analysis,
    check_data,
    check_evidence,
    check_state_files,
    run_checks,
    write_report,
)


def _config(tmp: Path, *, symbols: list[str], bars: dict[str, int] | None = None):
    (tmp / "state").mkdir(parents=True, exist_ok=True)
    (tmp / "journal").mkdir(parents=True, exist_ok=True)
    bars_dir = tmp / "bars"
    bars_dir.mkdir(parents=True, exist_ok=True)
    for stem, count in (bars or {}).items():
        rows = []
        for day in range(count):
            price = 100 + day
            rows.append(
                json.dumps(
                    {
                        "symbol": stem,
                        "start": f"2026-01-01T00:00:00+00:00" if day else
                                 f"2026-01-01T00:00:00+00:00",
                        "open": str(price),
                        "high": str(price),
                        "low": str(price),
                        "close": str(price),
                        "volume": "10",
                    }
                )
            )
        (bars_dir / f"{stem}_day.jsonl").write_text("\n".join(rows) + "\n")
    path = tmp / "agentic.toml"
    path.write_text(
        "\n".join(
            [
                'mode = "shadow"',
                f"symbol_whitelist = {json.dumps(symbols)}",
                'max_order_pct = "0.05"',
                'daily_notional_pct = "0.20"',
                'daily_loss_pct = "0.03"',
                "max_open_positions = 1",
                "equity_refresh_ticks = 30",
                "equity_refresh_seconds = 60",
                'timezone = "local"',
                f'history_path = "{bars_dir}"',
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


class StateCheckTests(unittest.TestCase):
    def test_missing_state_is_a_warning_not_a_failure(self) -> None:
        """A fresh install has no promotion file yet; that is not broken."""
        with tempfile.TemporaryDirectory() as name:
            config = _config(Path(name), symbols=["SPY"])
            check = check_state_files(config)
        self.assertEqual(check.status, WARN)
        self.assertIn("not created yet", check.detail)

    def test_a_corrupt_state_file_is_a_failure(self) -> None:
        with tempfile.TemporaryDirectory() as name:
            tmp = Path(name)
            config = _config(tmp, symbols=["SPY"])
            (tmp / "state" / "effective_limits.json").write_text("{broken")
            check = check_state_files(config)
        self.assertEqual(check.status, FAIL)
        self.assertIn("unparseable", check.detail)

    def test_a_budget_above_the_ceiling_is_a_failure(self) -> None:
        """The agent must never widen its own risk past what the operator set."""
        with tempfile.TemporaryDirectory() as name:
            tmp = Path(name)
            config = _config(tmp, symbols=["SPY"])
            (tmp / "state" / "effective_limits.json").write_text(
                json.dumps(
                    {
                        "max_order_pct": "0.9",
                        "daily_notional_pct": "0.9",
                        "reason": "tampered",
                        "updated_at": "now",
                        "confidence": "0.5",
                    }
                )
            )
            (tmp / "state" / "promotion.json").write_text(json.dumps({"stage": "shadow"}))
            (tmp / "state" / "risk_guard.json").write_text(json.dumps({"kill_switch": False}))
            check = check_state_files(config)
        self.assertEqual(check.status, FAIL)
        self.assertIn("exceeds the operator ceiling", check.detail)


class DataCheckTests(unittest.TestCase):
    def test_missing_bars_for_a_whitelisted_symbol_fail_the_check(self) -> None:
        with tempfile.TemporaryDirectory() as name:
            tmp = Path(name)
            config = _config(tmp, symbols=["SPY", "QQQ"], bars={"SPY": 300})
            check = check_data(config)
        self.assertEqual(check.status, FAIL)
        self.assertIn("QQQ", check.detail)

    def test_stale_bars_fail_the_check(self) -> None:
        with tempfile.TemporaryDirectory() as name:
            tmp = Path(name)
            config = _config(tmp, symbols=["SPY"], bars={"SPY": 300})
            path = Path(config.history_path) / "SPY_day.jsonl"
            rows = path.read_text().splitlines()
            # Rewrite every bar's timestamp to a year ago.
            path.write_text(
                "\n".join(
                    json.dumps(
                        {
                            **json.loads(row),
                            "start": "2025-01-01T00:00:00+00:00",
                        }
                    )
                    for row in rows
                )
                + "\n"
            )
            check = check_data(config)
        self.assertEqual(check.status, FAIL)
        self.assertIn("stale", check.detail)

    def test_healthy_history_passes(self) -> None:
        from datetime import datetime, timezone

        with tempfile.TemporaryDirectory() as name:
            tmp = Path(name)
            config = _config(tmp, symbols=["SPY"], bars={"SPY": 300})
            today = datetime.now(timezone.utc).date().isoformat()
            path = Path(config.history_path) / "SPY_day.jsonl"
            rows = [json.loads(row) for row in path.read_text().splitlines()]
            for index, row in enumerate(rows):
                rows[index] = {**row, "start": f"{today}T00:00:00+00:00"}
            path.write_text("\n".join(json.dumps(row) for row in rows) + "\n")
            check = check_data(config)
        self.assertIn(check.status, (OK, WARN))


class AnalysisCheckTests(unittest.TestCase):
    def test_the_analysis_path_is_exercised_not_just_the_files(self) -> None:
        from datetime import datetime, timezone

        with tempfile.TemporaryDirectory() as name:
            tmp = Path(name)
            config = _config(tmp, symbols=["SPY"], bars={"SPY": 400})
            today = datetime.now(timezone.utc).date().isoformat()
            path = Path(config.history_path) / "SPY_day.jsonl"
            rows = [json.loads(row) for row in path.read_text().splitlines()]
            path.write_text(
                "\n".join(
                    json.dumps({**row, "start": f"{today}T00:00:00+00:00"})
                    for row in rows
                )
                + "\n"
            )
            check = check_analysis(config)
        self.assertEqual(check.status, OK)
        self.assertIn("backtest ran", check.detail)

    def test_no_bars_is_a_failure_not_a_pass(self) -> None:
        with tempfile.TemporaryDirectory() as name:
            config = _config(Path(name), symbols=["SPY"])
            check = check_analysis(config)
        self.assertEqual(check.status, FAIL)


class EvidenceCheckTests(unittest.TestCase):
    """The check that ties the traded size back to the walk-forward evidence."""

    def _write(self, config, *, age_days: float, gate_pct: float | None) -> None:
        import json as _json
        from datetime import timedelta

        generated = datetime.now(timezone.utc) - timedelta(days=age_days)
        report = {
            "generated_at": generated.isoformat(),
            "drawdown_ceiling_pct": 15.0,
            "gate_size": (
                {"per_order_pct": gate_pct, "max_drawdown_pct": 13.9, "eligible": True}
                if gate_pct is not None
                else {"per_order_pct": None, "eligible": False, "reason": "none held"}
            ),
        }
        Path(config.state_dir, "strategy_evidence.json").write_text(
            _json.dumps(report)
        )

    def test_missing_report_warns_rather_than_passes(self) -> None:
        with tempfile.TemporaryDirectory() as name:
            config = _config(Path(name), symbols=["SPY"], bars={"SPY": 400})
            check = check_evidence(config)
        self.assertEqual(check.status, WARN)
        self.assertIn("walkforward", check.detail)

    def test_stale_report_warns(self) -> None:
        with tempfile.TemporaryDirectory() as name:
            config = _config(Path(name), symbols=["SPY"], bars={"SPY": 400})
            self._write(config, age_days=45, gate_pct=0.01)
            check = check_evidence(config)
        self.assertEqual(check.status, WARN)
        self.assertIn("days old", check.detail)

    def test_trading_bigger_than_the_evidence_supports_fails(self) -> None:
        """The whole point: a 5% book under a 13.9%-drawdown report must fail."""
        with tempfile.TemporaryDirectory() as name:
            tmp = Path(name)
            config = _config(tmp, symbols=["SPY"], bars={"SPY": 400})
            self._write(config, age_days=1, gate_pct=0.01)
            from agentic_trading.limits import Limits, save_limits

            save_limits(
                config.state_dir,
                Limits(
                    max_order_pct="0.05",
                    daily_notional_pct="0.20",
                    reason="test",
                    updated_at=datetime.now(timezone.utc).isoformat(),
                ),
            )
            check = check_evidence(config)
        self.assertEqual(check.status, FAIL)
        self.assertIn("drawdown ceiling", check.detail)

    def test_size_inside_the_gate_passes(self) -> None:
        with tempfile.TemporaryDirectory() as name:
            tmp = Path(name)
            config = _config(tmp, symbols=["SPY"], bars={"SPY": 400})
            self._write(config, age_days=1, gate_pct=0.01)
            from agentic_trading.limits import Limits, save_limits

            save_limits(
                config.state_dir,
                Limits(
                    max_order_pct="0.006",
                    daily_notional_pct="0.024",
                    reason="test",
                    updated_at=datetime.now(timezone.utc).isoformat(),
                ),
            )
            check = check_evidence(config)
        self.assertEqual(check.status, OK)


class ReportTests(unittest.TestCase):
    def test_offline_report_covers_state_data_and_analysis(self) -> None:
        with tempfile.TemporaryDirectory() as name:
            tmp = Path(name)
            config = _config(tmp, symbols=["SPY"], bars={"SPY": 400})
            report = run_checks(config, include_broker=False)
            path = write_report(config, report)
            payload = json.loads(path.read_text())

        names = {check.name for check in report.checks}
        self.assertEqual(
            names, {"state", "data", "analysis", "evidence", "plumbing"}
        )
        self.assertIn("healthy", payload)
        self.assertIn("checks", payload)


if __name__ == "__main__":
    unittest.main()
