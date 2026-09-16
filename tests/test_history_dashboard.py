"""Historical bar IO and the read-only dashboard surface."""

from __future__ import annotations

import json
import tempfile
import threading
import unittest
import urllib.request
from datetime import date, datetime, timezone
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
