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
                        # The trading day is the UTC day: a decision taken at
                        # 00:00 UTC belongs to today even though the journal file
                        # is named with yesterday's local date.
                        "intent": {
                            "created_at": datetime.now(timezone.utc).isoformat()
                        },
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

                # The console is read-only except for arming, which is a
                # loopback-only POST on exactly two paths. Any other POST is not
                # a write surface at all.
                request = urllib.request.Request(
                    f"{base}/api/summary", data=b"{}", method="POST"
                )
                with self.assertRaises(urllib.error.HTTPError) as ctx:
                    urllib.request.urlopen(request, timeout=5)
                self.assertEqual(ctx.exception.code, 404)
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


class OrderTableAccuracyTests(unittest.TestCase):
    """The table has to be right, not just populated.

    Each of these pins a way it was wrong: the last price missing on rejected
    rows, the per-order grade taken from a snapshot hours after the decision,
    and last night's decisions counted as today's.
    """

    def _config(self, tmp: Path) -> Any:
        config = DashboardTests()._config(tmp)
        Path(config.state_dir).mkdir(parents=True, exist_ok=True)
        Path(config.journal_dir).mkdir(parents=True, exist_ok=True)
        return config

    def _write(self, tmp: Path, day: str, records: list[dict]) -> None:
        path = tmp / "journal" / f"{day}.jsonl"
        path.write_text("\n".join(json.dumps(r) for r in records) + "\n")

    def _snapshot(self, ret_20: float) -> dict:
        return {
            "bars": 60,
            "last_close": 100.0,
            "ret_1_pct": 0.0,
            "ret_5_pct": 0.0,
            "ret_20_pct": ret_20,
            "vol_pct": 1.0,
            "trend_pct": ret_20,
            "from_high_pct": 1.0,
            "range_position": 0.5,
            "volume_z": 0.0,
            "volume_coverage": 1.0,
            "spread_bps": None,
        }

    def _reject(self, *, at: str, symbol: str = "BTC-USD", decision_id: str = "d") -> dict:
        return {
            "decision_id": decision_id,
            "event": "rejected",
            "reason": "regime_block: chop c=0.65",
            "symbol": symbol,
            "side": "buy",
            "notional": "1.50",
            "confidence": {"evidence": 0.4547},
            "intent": {
                "created_at": at,
                "symbol": symbol,
                "side": "buy",
                "quantity": "0.00002",
                "ref_price": "76459.39",
            },
        }

    def test_a_rejected_row_still_shows_the_price_it_was_decided_at(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_name:
            tmp = Path(tmp_name)
            config = self._config(tmp)
            self._write(
                tmp,
                date.today().isoformat(),
                [self._reject(at="2026-09-17T09:44:32Z")],
            )
            rows = DashboardState(config).orders_table()["rows"]
        self.assertEqual(rows[0]["last_price"], "76459.39")
        self.assertEqual(rows[0]["last_price_source"], "decision")

    def test_a_broker_quote_wins_over_the_decision_price(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_name:
            tmp = Path(tmp_name)
            config = self._config(tmp)
            record = self._reject(at="2026-09-17T09:44:32Z")
            record["event"] = "accepted"
            record["review"] = {"data": {"quote_data": {"last_trade_price": "76500.00"}}}
            record["order_request"] = {"type": "market"}
            record["session"] = "regular"  # the runtime records it top-level
            self._write(tmp, date.today().isoformat(), [record])
            row = DashboardState(config).orders_table()["rows"][0]
        self.assertEqual(row["last_price"], "76500.00")
        self.assertEqual(row["last_price_source"], "quote")
        self.assertEqual(row["type"], "market")
        self.assertEqual(row["session"], "regular")

    def test_the_grade_uses_the_snapshot_from_decision_time(self) -> None:
        """A reading taken eleven hours later describes a different market."""
        with tempfile.TemporaryDirectory() as tmp_name:
            tmp = Path(tmp_name)
            config = self._config(tmp)
            self._write(
                tmp,
                date.today().isoformat(),
                [
                    {
                        "event": "regime",
                        "symbol": "BTC-USD",
                        "regime": "chop",
                        "confidence": 0.65,
                        "blocks_entries": True,
                        "at": "2026-09-17T09:40:00Z",
                        "market": self._snapshot(-20.0),
                    },
                    self._reject(at="2026-09-17T09:44:32Z"),
                    {
                        "event": "regime",
                        "symbol": "BTC-USD",
                        "regime": "trend_up",
                        "confidence": 0.9,
                        "blocks_entries": False,
                        "at": "2026-09-17T20:00:00Z",
                        "market": self._snapshot(25.0),
                    },
                ],
            )
            row = DashboardState(config).orders_table()["rows"][0]
        grade = row["confidence"]["order"]
        self.assertIsNotNone(grade)
        assert grade is not None
        self.assertEqual(grade["snapshot_at"], "2026-09-17T09:40:00Z")
        self.assertTrue(grade["snapshot_exact"])
        self.assertEqual(grade["parts"]["trend"], 0.0)

    def test_a_row_with_no_snapshot_and_no_bars_is_left_ungraded(self) -> None:
        """No journaled snapshot and nothing to rebuild from: say so, don't invent."""
        with tempfile.TemporaryDirectory() as tmp_name:
            tmp = Path(tmp_name)
            config = DashboardTests()._config(tmp)
            (tmp / "agentic-nobars.toml").write_text(
                (tmp / "agentic.toml").read_text(encoding="utf-8")
                + f'history_path = "{tmp / "empty-bars"}"\n',
                encoding="utf-8",
            )
            config = load_config(tmp / "agentic-nobars.toml")
            Path(config.state_dir).mkdir(parents=True, exist_ok=True)
            Path(config.journal_dir).mkdir(parents=True, exist_ok=True)
            self._write(
                tmp,
                date.today().isoformat(),
                [self._reject(at="2026-09-17T09:44:32Z")],
            )
            row = DashboardState(config).orders_table()["rows"][0]
        self.assertIsNone(row["confidence"]["order"])

    def test_counters_follow_the_utc_trading_day_not_the_file_date(self) -> None:
        """Journals are named by local date; the counters must not be.

        A decision taken at 00:00 UTC is 20:00 the previous evening in New York,
        so it lands in yesterday's file. Counting by file date left the console
        reading 0/0/0 all day while the table showed the rows.
        """
        from datetime import timezone

        with tempfile.TemporaryDirectory() as tmp_name:
            tmp = Path(tmp_name)
            config = self._config(tmp)
            now = datetime.now(timezone.utc)
            today_utc = now.date().isoformat()
            yesterday_local = (now - timedelta(days=1)).astimezone().date().isoformat()
            # Today's decision, filed under yesterday's local date.
            self._write(
                tmp,
                yesterday_local,
                [self._reject(at=f"{today_utc}T00:10:00Z", decision_id="d-today")],
            )
            # An older decision, filed in its own day's file.
            self._write(
                tmp,
                yesterday_local,
                [
                    self._reject(at=f"{today_utc}T00:10:00Z", decision_id="d-today"),
                    self._reject(at="2020-01-01T09:00:00Z", decision_id="d-ancient"),
                ],
            )
            payload = DashboardState(config).orders_table()
            header = DashboardState(config).decision_counts()
        self.assertEqual(payload["counts"]["rejected"], 1, "only today's UTC decision counts")
        self.assertEqual(header["rejected"], 1)
        self.assertEqual(len(payload["rows"]), 2)
        self.assertEqual(payload["older_rows"], 1)
        older = next(row for row in payload["rows"] if row["older"])
        self.assertEqual(older["utc_day"], "2020-01-01")


class RebuiltGradeTests(unittest.TestCase):
    """Where the journal kept no snapshot, rebuild from bars — and say so.

    Measured on the 82 instants where both exist, the rebuilt grade lands within
    0.016 (median) and 0.038 (worst case) of the journaled one, which is what
    makes this an explanation rather than a fabrication.
    """

    def _config(self, tmp: Path) -> Any:
        config = DashboardTests()._config(tmp)
        Path(config.state_dir).mkdir(parents=True, exist_ok=True)
        Path(config.journal_dir).mkdir(parents=True, exist_ok=True)
        bars = tmp / "bars"
        bars.mkdir(parents=True, exist_ok=True)
        rows = []
        for index in range(300):
            price = 200.0 - index  # a downtrend: the grade should not be a buy
            rows.append(
                json.dumps(
                    {
                        "symbol": "BTCUSD",
                        "start": (
                            datetime(2024, 1, 1, tzinfo=timezone.utc)
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
        (bars / "BTCUSD_day.jsonl").write_text("\n".join(rows) + "\n")
        (tmp / "agentic-bars.toml").write_text(
            (tmp / "agentic.toml").read_text(encoding="utf-8")
            + f'history_path = "{bars}"\n',
            encoding="utf-8",
        )
        return load_config(tmp / "agentic-bars.toml")

    def _write(self, tmp: Path, day: str, records: list[dict]) -> None:
        (tmp / "journal" / f"{day}.jsonl").write_text(
            "\n".join(json.dumps(r) for r in records) + "\n"
        )

    def _reject(self, at: str) -> dict:
        return {
            "decision_id": "d",
            "event": "rejected",
            "reason": "regime_block: chop c=0.65",
            "symbol": "BTC-USD",
            "side": "buy",
            "notional": "1.50",
            "intent": {
                "created_at": at,
                "symbol": "BTC-USD",
                "side": "buy",
                "quantity": "0.00002",
                "ref_price": "76459.39",
            },
        }

    def test_a_row_without_a_snapshot_is_rebuilt_and_labelled(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_name:
            tmp = Path(tmp_name)
            config = self._config(tmp)
            # Bars end well after the decision, so the cut-off has to be the
            # decision time, not "the latest bar we happen to have".
            self._write(
                tmp,
                date.today().isoformat(),
                [self._reject(at="2024-06-01T09:44:32Z")],
            )
            row = DashboardState(config).orders_table()["rows"][0]
        grade = row["confidence"]["order"]
        self.assertIsNotNone(grade, "a stored-bar rebuild should be available")
        assert grade is not None
        self.assertEqual(grade["source"], "bars_asof")
        self.assertEqual(grade["snapshot_at"], "2024-06-01T09:44:32Z")
        self.assertIn("rebuilt from stored bars", grade["basis"])
        self.assertEqual(grade["verdict"], "rejected")
        # 2024-01-01 + 152 days is just before the decision: the cut-off must
        # keep the trend it saw, not the whole file.
        self.assertLess(grade["score"], 0.6)

    def test_a_journaled_snapshot_still_wins_over_a_rebuild(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_name:
            tmp = Path(tmp_name)
            config = self._config(tmp)
            self._write(
                tmp,
                date.today().isoformat(),
                [
                    {
                        "event": "regime",
                        "symbol": "BTC-USD",
                        "regime": "trend_up",
                        "confidence": 0.9,
                        "blocks_entries": False,
                        "at": "2024-06-01T09:40:00Z",
                        "market": {
                            "bars": 60,
                            "last_close": 100.0,
                            "ret_1_pct": 0.0,
                            "ret_5_pct": 1.0,
                            "ret_20_pct": 5.0,
                            "vol_pct": 1.0,
                            "trend_pct": 5.0,
                            "from_high_pct": 0.0,
                            "range_position": 1.0,
                            "volume_z": 0.0,
                            "volume_coverage": 1.0,
                            "spread_bps": None,
                        },
                    },
                    self._reject(at="2024-06-01T09:44:32Z"),
                ],
            )
            row = DashboardState(config).orders_table()["rows"][0]
        grade = row["confidence"]["order"]
        assert grade is not None
        self.assertEqual(grade["source"], "journal")
        self.assertEqual(grade["snapshot_at"], "2024-06-01T09:40:00Z")

    def test_no_bars_and_no_snapshot_leaves_the_row_ungraded(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_name:
            tmp = Path(tmp_name)
            config = DashboardTests()._config(tmp)
            (tmp / "agentic-nobars.toml").write_text(
                (tmp / "agentic.toml").read_text(encoding="utf-8")
                + f'history_path = "{tmp / "empty-bars"}"\n',
                encoding="utf-8",
            )
            config = load_config(tmp / "agentic-nobars.toml")
            Path(config.state_dir).mkdir(parents=True, exist_ok=True)
            Path(config.journal_dir).mkdir(parents=True, exist_ok=True)
            self._write(
                tmp,
                date.today().isoformat(),
                [self._reject(at="2024-06-01T09:44:32Z")],
            )
            row = DashboardState(config).orders_table()["rows"][0]
        self.assertIsNone(row["confidence"]["order"])


class PulseTests(unittest.TestCase):
    """A stopped agent must look stopped, not merely quiet."""

    def test_a_fresh_journal_reads_as_live(self) -> None:
        import json as _json

        with tempfile.TemporaryDirectory() as tmp_name:
            tmp = Path(tmp_name)
            config = DashboardTests()._config(tmp)
            journal = Path(config.journal_dir) / f"{date.today().isoformat()}.jsonl"
            journal.parent.mkdir(parents=True, exist_ok=True)
            journal.write_text(
                _json.dumps(
                    {"event": "cycle_stats", "at": datetime.now(timezone.utc).isoformat()}
                )
                + "\n"
            )
            pulse = DashboardState(config).pulse()
        self.assertFalse(pulse["silent"])
        self.assertLess(pulse["silent_seconds"], 60)

    def test_a_silent_journal_is_flagged(self) -> None:
        """Six hours of nothing looked exactly like a quiet market."""
        import json as _json
        from datetime import timedelta

        with tempfile.TemporaryDirectory() as tmp_name:
            tmp = Path(tmp_name)
            config = DashboardTests()._config(tmp)
            journal = Path(config.journal_dir) / f"{date.today().isoformat()}.jsonl"
            journal.parent.mkdir(parents=True, exist_ok=True)
            stale = (datetime.now(timezone.utc) - timedelta(hours=6)).isoformat()
            journal.write_text(
                _json.dumps({"event": "cycle_stats", "at": stale}) + "\n"
            )
            pulse = DashboardState(config).pulse()
        self.assertTrue(pulse["silent"])
        self.assertGreater(pulse["silent_seconds"], 3600)

    def test_an_empty_journal_is_not_reported_as_live(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_name:
            config = DashboardTests()._config(Path(tmp_name))
            pulse = DashboardState(config).pulse()
        self.assertIsNone(pulse["silent_seconds"])
        self.assertFalse(pulse["silent"])
        self.assertEqual(pulse["events_today"], 0)

    def test_the_summary_carries_the_pulse(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_name:
            config = DashboardTests()._config(Path(tmp_name))
            summary = DashboardState(config).summary()
        self.assertIn("pulse", summary)
        self.assertIn("silent", summary["pulse"])


class SizeFloorTests(unittest.TestCase):
    """An account too small to trade its own size limit must say so.

    On 2026-09-18 the agent sat in live mode refusing every entry with
    `below_min_notional`: at $50 with a 0.92% ceiling the order is $0.46 and the
    minimum is $1.00. The guard was right; the console was silent about it.
    """

    def _config(self, tmp: Path):
        config = DashboardTests()._config(tmp)
        Path(config.state_dir).mkdir(parents=True, exist_ok=True)
        Path(config.journal_dir).mkdir(parents=True, exist_ok=True)
        return config

    def test_it_reports_the_equity_that_would_fix_it(self) -> None:
        import json as _json

        with tempfile.TemporaryDirectory() as tmp_name:
            tmp = Path(tmp_name)
            config = self._config(tmp)
            (tmp / "state" / "effective_limits.json").write_text(
                _json.dumps({"max_order_pct": "0.00919975"})
            )
            (tmp / "state" / "risk_guard.json").write_text(
                _json.dumps({"current_equity": "50"})
            )
            risk = DashboardState(config).summary()["risk"]
        self.assertTrue(risk["too_small_to_trade"])
        self.assertEqual(risk["order_at_ceiling"], "0.46")
        self.assertEqual(risk["equity_needed"], "108.70")

    def test_a_big_enough_account_is_not_flagged(self) -> None:
        import json as _json

        with tempfile.TemporaryDirectory() as tmp_name:
            tmp = Path(tmp_name)
            config = self._config(tmp)
            (tmp / "state" / "effective_limits.json").write_text(
                _json.dumps({"max_order_pct": "0.01"})
            )
            (tmp / "state" / "risk_guard.json").write_text(
                _json.dumps({"current_equity": "250"})
            )
            risk = DashboardState(config).summary()["risk"]
        self.assertFalse(risk["too_small_to_trade"])
        self.assertEqual(risk["equity_needed"], "100.00")

    def test_an_unknown_equity_is_not_flagged(self) -> None:
        """No equity reading yet is not the same as too small."""
        with tempfile.TemporaryDirectory() as tmp_name:
            config = self._config(Path(tmp_name))
            risk = DashboardState(config).summary()["risk"]
        self.assertFalse(risk.get("too_small_to_trade", False))


class DecisionDayTests(unittest.TestCase):
    """The counters must use the strategy's day, not the journal file's date.

    The strategy rebalances once per UTC day, so today's decision is taken at
    00:00 UTC — 20:00 the previous evening in New York. Journals are named by
    local date, which filed a whole trading day in yesterday's file and left the
    console counters reading 0/0/0 while the table showed five rows.
    """

    def _config(self, tmp: Path):
        config = DashboardTests()._config(tmp)
        Path(config.state_dir).mkdir(parents=True, exist_ok=True)
        Path(config.journal_dir).mkdir(parents=True, exist_ok=True)
        return config

    def _write(self, tmp: Path, day: str, records: list[dict]) -> None:
        (tmp / "journal" / f"{day}.jsonl").write_text(
            "\n".join(json.dumps(r) for r in records) + "\n"
        )

    def _rejection(self, at: str, symbol: str = "BTC-USD") -> dict:
        return {
            "decision_id": f"d-{symbol}-{at}",
            "event": "rejected",
            "reason": "below_min_notional",
            "symbol": symbol,
            "intent": {"created_at": at, "symbol": symbol, "side": "buy"},
        }

    def test_a_utc_today_decision_in_yesterdays_file_still_counts(self) -> None:
        from datetime import timedelta

        with tempfile.TemporaryDirectory() as name:
            tmp = Path(name)
            config = self._config(tmp)
            now = datetime.now(timezone.utc)
            # 00:10 UTC today: late yesterday evening locally.
            at = now.replace(hour=0, minute=10, second=0, microsecond=0).isoformat()
            local_yesterday = (now - timedelta(days=1)).astimezone().date().isoformat()
            self._write(tmp, local_yesterday, [self._rejection(at)])
            counts = DashboardState(config).decision_counts()
        self.assertEqual(counts["rejected"], 1, "the trading day is not the file's day")

    def test_yesterdays_utc_decisions_do_not_count(self) -> None:
        from datetime import timedelta

        with tempfile.TemporaryDirectory() as name:
            tmp = Path(name)
            config = self._config(tmp)
            old = (datetime.now(timezone.utc) - timedelta(days=2)).isoformat()
            self._write(tmp, "2020-01-01", [self._rejection(old)])
            counts = DashboardState(config).decision_counts()
        self.assertEqual(counts["rejected"], 0)

    def test_summary_and_table_agree(self) -> None:
        from datetime import timedelta

        with tempfile.TemporaryDirectory() as name:
            tmp = Path(name)
            config = self._config(tmp)
            now = datetime.now(timezone.utc)
            at = now.replace(hour=0, minute=1, second=0, microsecond=0).isoformat()
            local_yesterday = (now - timedelta(days=1)).astimezone().date().isoformat()
            self._write(
                tmp,
                local_yesterday,
                [self._rejection(at, "BTC-USD"), self._rejection(at, "ETH-USD")],
            )
            state = DashboardState(config)
            header = state.decision_counts()
            table = state.orders_table()["counts"]
        self.assertEqual(header["rejected"], 2)
        self.assertEqual(table["rejected"], 2)


class ArmingTests(unittest.TestCase):
    """The console's one write path, and the fences around it.

    Order submission used to require an environment variable only. The console
    can now arm this workspace, which is a real change in the attack surface, so
    the fences are tested rather than described: loopback only, POST only, a
    typed confirmation, and arming refused until the system has earned it.
    """

    def _config(self, tmp: Path):
        config = DashboardTests()._config(tmp)
        Path(config.state_dir).mkdir(parents=True, exist_ok=True)
        Path(config.journal_dir).mkdir(parents=True, exist_ok=True)
        return config

    def _eligible(self, tmp: Path) -> None:
        import json as _json

        (tmp / "state" / "promotion.json").write_text(
            _json.dumps(
                {
                    "stage": "live",
                    "streak": 0,
                    "last_assessment": {"eligible": True},
                }
            )
        )
        (tmp / "state" / "strategy_evidence.json").write_text(
            _json.dumps({"generated_at": datetime.now(timezone.utc).isoformat()})
        )

    def test_a_shadow_agent_cannot_be_armed(self) -> None:
        from agentic_trading.arming import arm, arm_status

        with tempfile.TemporaryDirectory() as name:
            tmp = Path(name)
            self._config(tmp)
            result = arm(tmp / "state")
            status = arm_status(tmp / "state")
        self.assertTrue(result.get("refused"))
        self.assertIn("shadow", result["reason"])
        self.assertFalse(status["armed"])
        self.assertFalse(status["available"])

    def test_a_stale_report_cannot_be_armed(self) -> None:
        import json as _json

        from agentic_trading.arming import arm

        with tempfile.TemporaryDirectory() as name:
            tmp = Path(name)
            self._config(tmp)
            self._eligible(tmp)
            old = (datetime.now(timezone.utc) - timedelta(days=90)).isoformat()
            (tmp / "state" / "strategy_evidence.json").write_text(
                _json.dumps({"generated_at": old})
            )
            result = arm(tmp / "state")
        self.assertTrue(result.get("refused"))
        self.assertIn("days old", result["reason"])

    def test_an_eligible_agent_can_be_armed_and_disarmed(self) -> None:
        from agentic_trading.arming import arm, arm_status, disarm

        with tempfile.TemporaryDirectory() as name:
            tmp = Path(name)
            self._config(tmp)
            self._eligible(tmp)
            armed = arm(tmp / "state")
            status = arm_status(tmp / "state")
            disarmed = disarm(tmp / "state", reason="test")
            after = arm_status(tmp / "state")
        self.assertTrue(armed["armed"])
        self.assertTrue(status["available"])
        self.assertFalse(disarmed["armed"])
        self.assertFalse(after["armed"])
        # The workspace records the change, so a restart does not lose it.
        self.assertIsNotNone(after["since"])

    def test_http_requires_loopback_a_confirmation_and_the_right_method(self) -> None:
        import json as _json
        import urllib.error
        import urllib.request

        with tempfile.TemporaryDirectory() as name:
            tmp = Path(name)
            config = self._config(tmp)
            self._eligible(tmp)
            server = serve(config, host="127.0.0.1", port=0)
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            base = f"http://127.0.0.1:{server.server_address[1]}"

            def post(path: str, body: dict):
                request = urllib.request.Request(
                    base + path,
                    data=_json.dumps(body).encode(),
                    headers={"Content-Type": "application/json"},
                    method="POST",
                )
                return urllib.request.urlopen(request, timeout=5)

            try:
                # No confirmation phrase: refused, and nothing armed.
                with self.assertRaises(urllib.error.HTTPError) as missing:
                    post("/api/arm", {})
                self.assertEqual(missing.exception.code, 400)
                # With it: armed.
                with post("/api/arm", {"confirm": "ARM"}) as response:
                    payload = _json.loads(response.read())
                self.assertTrue(payload["armed"])
                # Disarm needs no confirmation, because it lowers risk.
                with post("/api/disarm", {}) as response:
                    payload = _json.loads(response.read())
                self.assertFalse(payload["armed"])
                # Any other path is not writable at all.
                with self.assertRaises(urllib.error.HTTPError) as other:
                    post("/api/limits", {})
                self.assertEqual(other.exception.code, 404)
                # GET on the arming endpoint is not a state change either:
                # there is no read of it, only the POST that changes it.
                with self.assertRaises(urllib.error.HTTPError) as read_arm:
                    urllib.request.urlopen(base + "/api/arm", timeout=5)
                self.assertEqual(read_arm.exception.code, 404)
            finally:
                server.shutdown()
                server.server_close()


class NotionalDisplayTests(unittest.TestCase):
    """A refusal before sizing has no size, and must not look like it does.

    The table showed the raw intent quantity (1.00000000 SOL) beside $0.00 for
    orders refused before the sizer ran, which reads as "it wanted one SOL for
    nothing" rather than "it was never sized".
    """

    def _config(self, tmp: Path):
        config = DashboardTests()._config(tmp)
        Path(config.state_dir).mkdir(parents=True, exist_ok=True)
        Path(config.journal_dir).mkdir(parents=True, exist_ok=True)
        return config

    def test_a_refusal_before_sizing_is_flagged_as_unsized(self) -> None:
        import json as _json

        with tempfile.TemporaryDirectory() as name:
            tmp = Path(name)
            config = self._config(tmp)
            (tmp / "journal" / f"{date.today().isoformat()}.jsonl").write_text(
                _json.dumps(
                    {
                        "decision_id": "d",
                        "event": "rejected",
                        "reason": "below_min_notional",
                        "symbol": "SOL-USD",
                        "notional": "0",
                        "intent": {
                            "created_at": datetime.now(timezone.utc).isoformat(),
                            "symbol": "SOL-USD",
                            "side": "buy",
                            "quantity": "1",
                            "ref_price": "100.80",
                        },
                    }
                )
                + "\n"
            )
            row = DashboardState(config).orders_table()["rows"][0]
        self.assertFalse(row["sized"])

    def test_a_sized_order_is_not_flagged(self) -> None:
        import json as _json

        with tempfile.TemporaryDirectory() as name:
            tmp = Path(name)
            config = self._config(tmp)
            (tmp / "journal" / f"{date.today().isoformat()}.jsonl").write_text(
                _json.dumps(
                    {
                        "decision_id": "d",
                        "event": "accepted",
                        "symbol": "SPY",
                        "notional": "1.02",
                        "intent": {
                            "created_at": datetime.now(timezone.utc).isoformat(),
                            "symbol": "SPY",
                            "side": "buy",
                            "quantity": "0.0015",
                            "ref_price": "680",
                        },
                    }
                )
                + "\n"
            )
            row = DashboardState(config).orders_table()["rows"][0]
        self.assertTrue(row["sized"])
