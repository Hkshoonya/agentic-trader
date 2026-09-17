"""Historical bar IO and the read-only dashboard surface."""

from __future__ import annotations

import json
import tempfile
import threading
import unittest
import urllib.request
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path

from agentic_trading.config import load_config
from agentic_trading.dashboard import DashboardState, serve
from agentic_trading.history import (
    Bar,
    aggregate_bars,
    bars_from_quotes,
    load_bars,
    load_bars_csv,
    parse_bars_payload,
    save_bars,
)
from agentic_trading.promotion import PromotionState, save_state


def _bar(index: int, close: str = "100") -> Bar:
    start = datetime(2025, 6, 2, 13, 30, tzinfo=timezone.utc)
    from datetime import timedelta

    start = start + timedelta(minutes=5 * index)
    price = Decimal(close)
    return Bar("SPY", start, price, price + 1, price - 1, price)


class BarParsingTests(unittest.TestCase):
    def test_parses_nested_bars_payload(self) -> None:
        payload = {
            "data": {
                "bars": [
                    {
                        "begins_at": "2025-06-02T13:30:00Z",
                        "open_price": "100.0",
                        "high_price": "101.0",
                        "low_price": "99.5",
                        "close_price": "100.5",
                        "volume": "1200",
                    }
                ]
            }
        }
        bars = parse_bars_payload(payload, symbol="spy")
        self.assertEqual(len(bars), 1)
        self.assertEqual(bars[0].symbol, "SPY")
        self.assertEqual(bars[0].close, Decimal("100.5"))
        self.assertEqual(bars[0].volume, Decimal("1200"))

    def test_parses_list_with_epoch_timestamps(self) -> None:
        bars = parse_bars_payload(
            [{"timestamp": 1748871000, "open": "1", "high": "2", "low": "0.5", "close": "1.5"}],
            symbol="SPY",
        )
        self.assertEqual(len(bars), 1)
        self.assertEqual(bars[0].open, Decimal("1"))

    def test_unknown_shape_raises(self) -> None:
        with self.assertRaises(Exception):
            parse_bars_payload({"unexpected": []}, symbol="SPY")

    def test_save_and_load_roundtrip(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "bars.jsonl"
            self.assertEqual(save_bars(path, [_bar(0, "100"), _bar(1, "101")]), 2)
            loaded = load_bars(path)
            self.assertEqual(len(loaded), 2)
            self.assertEqual(loaded[1].close, Decimal("101"))
            self.assertEqual(loaded[0].start, _bar(0).start)

    def test_csv_import(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "bars.csv"
            path.write_text(
                "date,open,high,low,close,volume\n"
                "2025-06-02,100,101,99,100.5,1000\n"
                "2025-06-03,100.5,102,100,101.5,1200\n",
                encoding="utf-8",
            )
            bars = load_bars_csv(path, symbol="spy")
            self.assertEqual(len(bars), 2)
            self.assertEqual(bars[0].symbol, "SPY")
            self.assertEqual(bars[1].close, Decimal("101.5"))

    def test_aggregation_buckets_by_time(self) -> None:
        bars = [_bar(i, str(100 + i)) for i in range(6)]
        aggregated = aggregate_bars(bars, minutes=15)
        self.assertEqual(len(aggregated), 2)
        self.assertEqual(aggregated[0].open, bars[0].open)
        self.assertEqual(aggregated[0].close, bars[2].close)
        self.assertEqual(aggregated[1].close, bars[5].close)

    def test_quotes_convert_to_flat_bars(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "quotes.jsonl"
            path.write_text(
                json.dumps(
                    {
                        "symbol": "SPY",
                        "observed_at": "2026-09-15T07:58:49Z",
                        "quote_at": "2026-09-15T07:58:47Z",
                        "bid": "100.00",
                        "ask": "100.10",
                    }
                )
                + "\n",
                encoding="utf-8",
            )
            bars = bars_from_quotes(path)
            self.assertEqual(len(bars), 1)
            self.assertEqual(bars[0].close, Decimal("100.05"))


class DashboardTests(unittest.TestCase):
    def _config(self, tmp: Path):
        config_path = tmp / "agentic.toml"
        config_path.write_text(
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
                    f'quotes_path = "{tmp / "quotes.jsonl"}"',
                    f'journal_dir = "{tmp / "journal"}"',
                    f'state_dir = "{tmp / "state"}"',
                    f'tools_snapshot_path = "{tmp / "tools.json"}"',
                    f'token_path = "{tmp / "tok.json"}"',
                    'mcp_url = "https://agent.robinhood.com/mcp/trading"',
                    'autonomy = "auto"',
                ]
            )
            + "\n",
            encoding="utf-8",
        )
        return load_config(config_path)

    def test_summary_reads_journal_and_promotion_state(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_name:
            tmp = Path(tmp_name)
            config = self._config(tmp)
            journal = Path(config.journal_dir) / f"{date.today().isoformat()}.jsonl"
            journal.parent.mkdir(parents=True, exist_ok=True)
            journal.write_text(
                json.dumps(
                    {
                        "event": "accepted",
                        "notional": "5.00",
                        "symbol": "SPY",
                        "side": "buy",
                        "mode": "shadow",
                        "intent": {"created_at": "2026-09-16T14:00:00Z"},
                    }
                )
                + "\n",
                encoding="utf-8",
            )
            save_state(config.state_dir, PromotionState(stage="probation", streak=2))

            state = DashboardState(config)
            summary = state.summary()
            self.assertEqual(summary["mode"], "shadow")
            self.assertEqual(summary["promotion"]["stage"], "probation")
            self.assertEqual(summary["event_counts"]["accepted"], 1)
            self.assertEqual(summary["autonomy"], "auto")
            curve = state.equity_curve()
            self.assertEqual(len(curve["points"]), 1)
            self.assertEqual(curve["points"][0]["notional"], 5.0)

    def test_edited_config_is_picked_up_without_a_restart(self) -> None:
        """The console outlives config edits; it must not report stale limits."""
        with tempfile.TemporaryDirectory() as tmp_name:
            tmp = Path(tmp_name)
            config_path = tmp / "agentic.toml"
            self._config(tmp)

            state = DashboardState(
                load_config(config_path), config_path=config_path
            )
            self.assertEqual(state.summary()["symbols"], ["SPY"])
            self.assertEqual(state.summary()["session_policy"], "regular")

            text = config_path.read_text(encoding="utf-8")
            config_path.write_text(
                text.replace('symbol_whitelist = ["SPY"]', 'symbol_whitelist = ["BTC-USD"]')
                .replace('strategy = "fixture"', 'strategy = "trend_crypto"')
                + 'session_policy = "any"\n'
                + 'strategy = "trend_crypto"\n',
                encoding="utf-8",
            )

            summary = state.summary()
            self.assertEqual(summary["symbols"], ["BTC-USD"])
            self.assertEqual(summary["strategy"], "trend_crypto")
            self.assertEqual(summary["session_policy"], "any")

    def test_unreadable_config_keeps_serving_the_last_good_copy(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_name:
            tmp = Path(tmp_name)
            config_path = tmp / "agentic.toml"
            self._config(tmp)
            state = DashboardState(load_config(config_path), config_path=config_path)

            config_path.write_text("this is not = = toml\n", encoding="utf-8")

            summary = state.summary()
            self.assertEqual(summary["symbols"], ["SPY"])

    def test_non_finite_metrics_cannot_break_the_console(self) -> None:
        """A backtest with no losing trades reports an infinite profit factor.

        Python serializes that as ``Infinity``, which no browser can parse: the
        whole console rendered as placeholders until the wire was made strict.
        """
        with tempfile.TemporaryDirectory() as tmp_name:
            tmp = Path(tmp_name)
            config = self._config(tmp)
            state_dir = Path(config.state_dir)
            state_dir.mkdir(parents=True, exist_ok=True)
            (state_dir / "evolution.json").write_text(
                '{"out_of_sample": {"profit_factor": Infinity}, '
                '"in_sample": {"profit_factor": 2.5}}\n',
                encoding="utf-8",
            )

            server = serve(config, host="127.0.0.1", port=0)
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            base = f"http://127.0.0.1:{server.server_address[1]}"
            try:
                with urllib.request.urlopen(f"{base}/api/summary", timeout=5) as r:
                    body = r.read().decode("utf-8")
            finally:
                server.shutdown()
                server.server_close()

            # json.loads refuses bare Infinity, so parsing at all is the assert.
            payload = json.loads(body)
            metrics = payload["evolution"]["out_of_sample"]
            self.assertIsNone(metrics["profit_factor"])
            self.assertEqual(payload["evolution"]["in_sample"]["profit_factor"], 2.5)

    def test_order_rows_carry_both_confidences(self) -> None:
        """Each order shows how sure the model was and how real the edge looked.

        The advisor's opinion is journaled under its own decision_id, so the
        table has to join it back rather than lose it.
        """
        with tempfile.TemporaryDirectory() as tmp_name:
            tmp = Path(tmp_name)
            config = self._config(tmp)
            journal = Path(config.journal_dir) / f"{date.today().isoformat()}.jsonl"
            journal.parent.mkdir(parents=True, exist_ok=True)
            journal.write_text(
                "\n".join(
                    [
                        json.dumps(
                            {
                                "decision_id": "d-1",
                                "event": "advisor",
                                "model": "deepseek-flash",
                                "action": "allow",
                                "confidence": 0.62,
                            }
                        ),
                        json.dumps(
                            {
                                "decision_id": "d-1",
                                "event": "rejected",
                                "reason": "max_open_positions",
                                "symbol": "BTC-USD",
                                "side": "buy",
                                "notional": "1.25",
                                "confidence": {"evidence": 0.5252},
                                "intent": {"created_at": "2026-09-16T14:00:00Z"},
                            }
                        ),
                    ]
                )
                + "\n",
                encoding="utf-8",
            )

            rows = DashboardState(config).orders_table()["rows"]

        self.assertEqual(len(rows), 1)
        confidences = rows[0]["confidence"]
        self.assertEqual(confidences["advisor"], 0.62)
        self.assertEqual(confidences["advisor_action"], "allow")
        self.assertEqual(confidences["evidence"], 0.5252)
        self.assertEqual(rows[0]["reason"], "max_open_positions")

    def test_a_decision_made_before_grading_existed_is_still_graded(self) -> None:
        """The journal's regime record holds the snapshot; grade from that.

        Without this the operator sees only the flat strategy-level `ev` number
        beside every old row — the exact complaint that led to per-order grades.
        """
        with tempfile.TemporaryDirectory() as tmp_name:
            tmp = Path(tmp_name)
            config = self._config(tmp)
            journal = Path(config.journal_dir) / f"{date.today().isoformat()}.jsonl"
            journal.parent.mkdir(parents=True, exist_ok=True)
            snapshot = {
                "bars": 60,
                "last_close": 76528.79,
                "ret_1_pct": 0.5,
                "ret_5_pct": -0.95,
                "ret_20_pct": -1.68,
                "vol_pct": 2.07,
                "trend_pct": -3.44,
                "from_high_pct": 5.83,
                "range_position": 0.74,
                "volume_z": -0.5,
                "volume_coverage": 1.0,
                "spread_bps": None,
            }
            journal.write_text(
                "\n".join(
                    [
                        json.dumps(
                            {
                                "event": "regime",
                                "symbol": "BTC-USD",
                                "regime": "chop",
                                "confidence": 0.65,
                                "blocks_entries": True,
                                "market": snapshot,
                            }
                        ),
                        json.dumps(
                            {
                                "decision_id": "d-2",
                                "event": "rejected",
                                "reason": "regime_block: chop c=0.65",
                                "symbol": "BTC-USD",
                                "side": "buy",
                                "notional": "1.45",
                                "confidence": {"evidence": 0.4547},
                                "intent": {"created_at": "2026-09-17T09:44:32Z"},
                            }
                        ),
                    ]
                )
                + "\n",
                encoding="utf-8",
            )

            rows = DashboardState(config).orders_table()["rows"]

        self.assertEqual(len(rows), 1)
        grade = rows[0]["confidence"]["order"]
        self.assertIsNotNone(grade, "the journaled snapshot must be gradeable")
        assert grade is not None
        self.assertEqual(grade["verdict"], "rejected")
        self.assertEqual(grade["source"], "journal")
        self.assertLess(grade["score"], 0.5, "a chop-blocked downtrend is not a buy")
        self.assertIn("trend", grade["notes"])

    def test_a_row_with_no_snapshot_is_not_graded(self) -> None:
        """No observed features means no grade — not an invented one."""
        with tempfile.TemporaryDirectory() as tmp_name:
            tmp = Path(tmp_name)
            config = self._config(tmp)
            journal = Path(config.journal_dir) / f"{date.today().isoformat()}.jsonl"
            journal.parent.mkdir(parents=True, exist_ok=True)
            journal.write_text(
                json.dumps(
                    {
                        "decision_id": "d-3",
                        "event": "rejected",
                        "reason": "max_orders_per_day",
                        "symbol": "SPY",
                        "side": "buy",
                        "notional": "1.00",
                    }
                )
                + "\n",
                encoding="utf-8",
            )

            rows = DashboardState(config).orders_table()["rows"]

        self.assertIsNone(rows[0]["confidence"]["order"])


    def test_http_surface_is_read_only(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_name:
            tmp = Path(tmp_name)
            config = self._config(tmp)
            server = serve(config, host="127.0.0.1", port=0)
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            base = f"http://127.0.0.1:{server.server_address[1]}"
            try:
                with urllib.request.urlopen(f"{base}/", timeout=5) as response:
                    html = response.read().decode("utf-8")
                self.assertIn("Agentic Trader", html)
                self.assertIn("/api/summary", html)

                with urllib.request.urlopen(f"{base}/api/summary", timeout=5) as r:
                    summary = json.loads(r.read().decode("utf-8"))
                self.assertIn("promotion", summary)
                self.assertIn("session", summary)

                with urllib.request.urlopen(f"{base}/api/journal?offset=0", timeout=5) as r:
                    feed = json.loads(r.read().decode("utf-8"))
                self.assertEqual(feed["records"], [])

                with urllib.request.urlopen(f"{base}/api/health", timeout=5) as r:
                    self.assertTrue(json.loads(r.read().decode("utf-8"))["ok"])

                request = urllib.request.Request(
                    f"{base}/api/summary", data=b"{}", method="POST"
                )
                with self.assertRaises(urllib.error.HTTPError) as ctx:
                    urllib.request.urlopen(request, timeout=5)
                self.assertEqual(ctx.exception.code, 501)
            finally:
                server.shutdown()
                server.server_close()


if __name__ == "__main__":
    unittest.main()


def _with_history(tmp: Path) -> Path:
    """The shared test config, plus a bar directory the console can read."""
    path = tmp / "agentic.toml"
    with_history = tmp / "agentic-bars.toml"
    with_history.write_text(
        path.read_text(encoding="utf-8") + f'history_path = "{tmp / "bars"}"\n',
        encoding="utf-8",
    )
    return with_history


class CadenceTests(unittest.TestCase):
    """Why the order table is static while the stream keeps moving."""

    def _config(self, tmp: Path):
        return DashboardTests()._config(tmp)

    def test_reports_the_daily_rebalance_and_the_next_one(self) -> None:
        import json as _json

        with tempfile.TemporaryDirectory() as tmp_name:
            tmp = Path(tmp_name)
            config = self._config(tmp)
            Path(config.state_dir).mkdir(parents=True, exist_ok=True)
            (Path(config.state_dir) / f"strategy_{config.strategy}.json").write_text(
                _json.dumps(
                    {
                        "last_decision_date": date.today().isoformat(),
                        "quantities": {"BTCUSD": "0.000016"},
                    }
                )
            )
            cadence = DashboardState(config).cadence()
        self.assertEqual(cadence["rebalance"], "daily_utc")
        self.assertTrue(cadence["rebalanced_today"])
        self.assertEqual(cadence["held"], ["BTCUSD"])
        # Already decided today: the next decision is the next UTC midnight.
        self.assertTrue(cadence["next_decision_at"].endswith("T00:00:00+00:00"))
        self.assertIn("static by design", cadence["explanation"])

    def test_a_day_with_no_decision_is_due_now(self) -> None:
        import json as _json

        with tempfile.TemporaryDirectory() as tmp_name:
            tmp = Path(tmp_name)
            config = self._config(tmp)
            Path(config.state_dir).mkdir(parents=True, exist_ok=True)
            (Path(config.state_dir) / f"strategy_{config.strategy}.json").write_text(
                _json.dumps({"last_decision_date": "2020-01-01", "quantities": {}})
            )
            cadence = DashboardState(config).cadence()
        self.assertFalse(cadence["rebalanced_today"])
        self.assertEqual(cadence["held"], [])

    def test_the_last_decision_time_comes_from_the_journal(self) -> None:
        import json as _json

        with tempfile.TemporaryDirectory() as tmp_name:
            tmp = Path(tmp_name)
            config = self._config(tmp)
            journal = Path(config.journal_dir) / f"{date.today().isoformat()}.jsonl"
            journal.parent.mkdir(parents=True, exist_ok=True)
            journal.write_text(
                _json.dumps(
                    {
                        "decision_id": "d-1",
                        "event": "rejected",
                        "reason": "regime_block",
                        "symbol": "BTC-USD",
                        "intent": {"created_at": "2026-09-17T09:44:32Z"},
                    }
                )
                + "\n"
            )
            cadence = DashboardState(config).cadence()
        self.assertEqual(cadence["last_decision_at"], "2026-09-17T09:44:32Z")


class CandidateTests(unittest.TestCase):
    """The live "what would it trade now" panel."""

    def _config_with_bars(self, tmp: Path):
        config = DashboardTests()._config(tmp)
        Path(config.state_dir).mkdir(parents=True, exist_ok=True)
        (tmp / "bars").mkdir(parents=True, exist_ok=True)
        config = load_config(_with_history(tmp))
        bars = Path(config.history_path)
        rows = []
        for index in range(300):
            price = 100 + index
            rows.append(
                json.dumps(
                    {
                        "symbol": "SPY",
                        "start": (
                            datetime(2020, 1, 1, tzinfo=timezone.utc)
                            + timedelta(days=index)
                        ).isoformat(),
                        "open": str(price),
                        "high": str(price),
                        "low": str(price),
                        "close": str(price),
                        "volume": "10",
                    }
                )
            )
        (bars / "SPY_day.jsonl").write_text("\n".join(rows) + "\n")
        return config

    def test_candidates_show_selection_and_reasons(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_name:
            tmp = Path(tmp_name)
            config = self._config_with_bars(tmp)
            payload = DashboardState(config).candidates()
        self.assertTrue(payload["rows"])
        spy = next(row for row in payload["rows"] if row["symbol"] == "SPY")
        self.assertTrue(spy["selected"], spy.get("reason"))
        self.assertIn("vote", spy["reason"])
        self.assertIsNotNone(spy["vol_pct"])

    def test_candidates_are_cached_between_polls(self) -> None:
        """The panel reads 16 bar files; the 2-second summary must not."""
        with tempfile.TemporaryDirectory() as tmp_name:
            tmp = Path(tmp_name)
            config = self._config_with_bars(tmp)
            state = DashboardState(config)
            first = state.candidates()
            second = state.candidates()
        self.assertEqual(first["generated_at"], second["generated_at"])
        self.assertEqual(state._candidate_summary()["generated_at"], first["generated_at"])

    def test_a_held_symbol_is_flagged(self) -> None:
        import json as _json

        with tempfile.TemporaryDirectory() as tmp_name:
            tmp = Path(tmp_name)
            config = self._config_with_bars(tmp)
            Path(config.state_dir).mkdir(parents=True, exist_ok=True)
            (Path(config.state_dir) / f"strategy_{config.strategy}.json").write_text(
                _json.dumps({"last_decision_date": "2020-01-01", "quantities": {"SPY": "1"}})
            )
            payload = DashboardState(config).candidates()
        spy = next(row for row in payload["rows"] if row["symbol"] == "SPY")
        self.assertTrue(spy["held"])

    def test_http_endpoint_is_served(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_name:
            tmp = Path(tmp_name)
            config = self._config_with_bars(tmp)
            server = serve(config, host="127.0.0.1", port=0)
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            base = f"http://127.0.0.1:{server.server_address[1]}"
            try:
                with urllib.request.urlopen(f"{base}/api/candidates", timeout=5) as resp:
                    payload = json.loads(resp.read().decode("utf-8"))
            finally:
                server.shutdown()
                server.server_close()
        self.assertIn("rows", payload)
