"""History refresh: the evidence has to be able to change."""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from agentic_trading.config import load_config
from agentic_trading.history_sync import (
    bar_stem,
    check_records,
    coinbase_row_to_record,
    fingerprint,
    merge_records,
    read_records,
    sync_history,
    sync_symbol,
)
from agentic_trading.selfimprove import history_plan


def bar(start: str, close: str) -> dict:
    return {
        "symbol": "BTCUSD",
        "start": start,
        "open": close,
        "high": close,
        "low": close,
        "close": close,
        "volume": "1",
    }


class MergeTests(unittest.TestCase):
    def test_merge_appends_new_bars_and_keeps_the_old_ones(self) -> None:
        existing = [bar("2026-09-14T00:00:00+00:00", "100")]
        incoming = [
            bar("2026-09-15T00:00:00+00:00", "101"),
            bar("2026-09-16T00:00:00+00:00", "102"),
        ]
        merged, added = merge_records(existing, incoming)
        self.assertEqual(added, 2)
        self.assertEqual([row["start"][:10] for row in merged],
                         ["2026-09-14", "2026-09-15", "2026-09-16"])

    def test_a_refetched_bar_replaces_rather_than_duplicates(self) -> None:
        """The newest fetch wins: an intraday partial candle is not frozen."""
        existing = [bar("2026-09-16T00:00:00+00:00", "100")]
        incoming = [bar("2026-09-16T00:00:00+00:00", "108")]
        merged, added = merge_records(existing, incoming)
        self.assertEqual(added, 0)
        self.assertEqual(len(merged), 1)
        self.assertEqual(merged[0]["close"], "108")

    def test_coinbase_rows_map_to_bar_records(self) -> None:
        # [time, low, high, open, close, volume]
        record = coinbase_row_to_record("BTC-USD", [1789603200, 100.5, 110.0, 101.0, 109.0, 12.5])
        self.assertEqual(record["symbol"], "BTCUSD")
        self.assertEqual(record["open"], "101.0")
        self.assertEqual(record["close"], "109.0")
        self.assertTrue(record["start"].startswith("2026-"))
        self.assertIsNone(coinbase_row_to_record("BTC-USD", ["nonsense"]))

    def test_bar_stem_matches_the_file_naming(self) -> None:
        self.assertEqual(bar_stem("BTC-USD"), "BTCUSD")
        self.assertEqual(bar_stem("spy"), "SPY")


class SyncSymbolTests(unittest.TestCase):
    def test_sync_extends_an_existing_file(self) -> None:
        with tempfile.TemporaryDirectory() as name:
            path = Path(name) / "BTCUSD_day.jsonl"
            path.write_text(json.dumps(bar("2026-09-14T00:00:00+00:00", "100")) + "\n")

            result = sync_symbol(
                path,
                symbol="BTC-USD",
                fetch=lambda: [bar("2026-09-16T00:00:00+00:00", "102")],
            )

            self.assertEqual(result.added, 1)
            self.assertEqual(result.total, 2)
            self.assertEqual(len(read_records(path)), 2)
            self.assertEqual(path.read_text().count("\n"), 2)

    def test_an_empty_fetch_leaves_the_file_alone(self) -> None:
        with tempfile.TemporaryDirectory() as name:
            path = Path(name) / "BTCUSD_day.jsonl"
            path.write_text(json.dumps(bar("2026-09-14T00:00:00+00:00", "100")) + "\n")
            self.assertIsNone(
                sync_symbol(path, symbol="BTC-USD", fetch=lambda: [])
            )
            self.assertEqual(len(read_records(path)), 1)


def _config(tmp: Path):
    cfg = tmp / "agentic.toml"
    cfg.write_text(
        "\n".join(
            [
                'mode = "shadow"',
                'symbol_whitelist = ["SPY", "BTC-USD", "DOGE-USD"]',
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


class SyncHistoryTests(unittest.TestCase):
    def test_only_whitelisted_symbols_with_files_are_refreshed(self) -> None:
        class _Broker:
            def __init__(self) -> None:
                self.calls: list[str] = []

            def get_historicals(self, symbols, **_kwargs):
                self.calls.append(symbols[0])
                return {
                    "data": {
                        "results": [
                            {
                                "symbol": symbols[0],
                                "interval": "day",
                                "bars": [
                                    {
                                        "begins_at": "2026-09-16T00:00:00Z",
                                        "open_price": "1",
                                        "high_price": "2",
                                        "low_price": "0.5",
                                        "close_price": "1.5",
                                        "volume": "10",
                                    }
                                ],
                            }
                        ]
                    }
                }

        with tempfile.TemporaryDirectory() as name:
            tmp = Path(name)
            config = _config(tmp)
            bars = Path(config.history_path)
            bars.mkdir(parents=True, exist_ok=True)
            for stem in ("SPY", "BTCUSD", "ETHUSD"):  # ETH is not whitelisted
                (bars / f"{stem}_day.jsonl").write_text(
                    json.dumps(bar("2026-09-15T00:00:00+00:00", "100")) + "\n"
                )
            broker = _Broker()

            crypto_calls: list[str] = []

            def crypto(symbol: str):
                crypto_calls.append(symbol)
                return [bar("2026-09-16T00:00:00+00:00", "200")]

            results, errors = sync_history(
                config, broker, crypto_fetch=crypto
            )

        self.assertEqual(errors, [])
        self.assertEqual(broker.calls, ["SPY"])
        self.assertEqual(crypto_calls, ["BTC-USD"])
        self.assertEqual({r.symbol for r in results}, {"SPY", "BTC-USD"})

    def test_one_broken_symbol_does_not_stop_the_others(self) -> None:
        class _Broker:
            def get_historicals(self, symbols, **_kwargs):
                raise RuntimeError("broker said no")

        with tempfile.TemporaryDirectory() as name:
            tmp = Path(name)
            config = _config(tmp)
            bars = Path(config.history_path)
            bars.mkdir(parents=True, exist_ok=True)
            (bars / "SPY_day.jsonl").write_text(
                json.dumps(bar("2026-09-15T00:00:00+00:00", "100")) + "\n"
            )
            (bars / "BTCUSD_day.jsonl").write_text(
                json.dumps(bar("2026-09-15T00:00:00+00:00", "100")) + "\n"
            )

            results, errors = sync_history(
                config, _Broker(), crypto_fetch=lambda _s: [bar("2026-09-16T00:00:00+00:00", "2")]
            )

        self.assertEqual([r.symbol for r in results], ["BTC-USD"])
        self.assertEqual(len(errors), 1)
        self.assertIn("broker said no", errors[0])


class HistoryPlanTests(unittest.TestCase):

    def test_quality_flags_duplicates_spikes_and_missing_volume(self) -> None:
        # No volume column at all, plus a duplicate and an impossible move.
        def bare(start: str, close: str) -> dict:
            record = bar(start, close)
            record.pop("volume")
            return record

        records = [bare("2026-09-14T00:00:00+00:00", "100")]
        records.append(bare("2026-09-14T00:00:00+00:00", "100"))  # duplicate
        records.append(bare("2026-09-15T00:00:00+00:00", "500"))  # +400% spike
        report = check_records("BTC-USD", records)
        self.assertEqual(report.bars, 3)
        self.assertFalse(report.volume_usable)
        joined = " | ".join(report.issues)
        self.assertIn("duplicate", joined)
        self.assertIn("60%", joined)
        self.assertIn("volume", joined)

    def test_quality_passes_a_clean_file(self) -> None:
        records = []
        for day in range(1, 25):
            record = bar(f"2026-08-{day:02d}T00:00:00+00:00", str(100 + day))
            record["volume"] = "10"
            records.append(record)
        report = check_records("BTC-USD", records)
        self.assertEqual(report.issues, [])
        self.assertTrue(report.volume_usable)

    def test_quality_flags_a_week_long_hole(self) -> None:
        records = [
            bar("2026-08-01T00:00:00+00:00", "100"),
            bar("2026-09-01T00:00:00+00:00", "101"),
        ]
        report = check_records("BTC-USD", records)
        self.assertTrue(any("gap" in issue for issue in report.issues))

    def test_the_evaluation_universe_is_the_traded_whitelist(self) -> None:
        """Grading an edge on instruments the agent may not trade is a lie."""
        with tempfile.TemporaryDirectory() as name:
            tmp = Path(name)
            config = _config(tmp)
            bars = Path(config.history_path)
            bars.mkdir(parents=True, exist_ok=True)
            for stem in ("SPY", "BTCUSD", "DOGEUSD", "UNIUSD", "AAVEUSD"):
                (bars / f"{stem}_day.jsonl").write_text(
                    json.dumps(bar("2026-09-15T00:00:00+00:00", "100")) + "\n"
                )

            interval, available, selected = history_plan(bars, config)

        self.assertEqual(interval, "day")
        self.assertIn("UNIUSD", available)
        self.assertEqual(sorted(selected), ["BTCUSD", "DOGEUSD", "SPY"])

    def test_fingerprint_changes_only_when_files_change(self) -> None:
        with tempfile.TemporaryDirectory() as name:
            bars = Path(name)
            (bars / "SPY_day.jsonl").write_text("{}\n")
            first = fingerprint(bars, ["SPY"])
            self.assertEqual(first, fingerprint(bars, ["SPY"]))
            (bars / "SPY_day.jsonl").write_text("{}\n{}\n")
            self.assertNotEqual(first, fingerprint(bars, ["SPY"]))


if __name__ == "__main__":
    unittest.main()
