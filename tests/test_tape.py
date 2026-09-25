"""The quote tape: clean intraday data banked from every poll."""

from __future__ import annotations

import gzip
import json
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path

from agentic_trading.tape import QuoteTape

T0 = datetime(2026, 9, 24, 1, 0, 1, tzinfo=timezone.utc)


def _quote(bid: str, observed: str = "2026-09-24T01:00:01Z") -> dict:
    return {
        "symbol": "BTC-USD",
        "bid": Decimal(bid),
        "ask": Decimal(bid) + Decimal("0.2"),
        "quote_at": observed,
        "observed_at": observed,
    }


def _rows(path: Path) -> list[dict]:
    with gzip.open(path, "rt", encoding="utf-8") as handle:
        return [json.loads(line) for line in handle]


class QuoteTapeTests(unittest.TestCase):
    def test_quotes_append_to_a_daily_gzip_file(self) -> None:
        with tempfile.TemporaryDirectory() as name:
            tape = QuoteTape(Path(name), flush_seconds=0)
            self.assertIsNone(tape.record([_quote("100.5")], now=T0))
            self.assertIsNone(tape.record([_quote("101")], now=T0))
            rows = _rows(Path(name) / "BTC-USD" / "2026-09-24.jsonl.gz")
        self.assertEqual([row["bid"] for row in rows], ["100.5", "101"])
        self.assertEqual(
            set(rows[0]), {"symbol", "bid", "ask", "quote_at", "observed_at"}
        )

    def test_the_file_is_named_by_the_quote_day_not_the_clock(self) -> None:
        with tempfile.TemporaryDirectory() as name:
            tape = QuoteTape(Path(name), flush_seconds=0)
            tape.record([_quote("1", observed="2026-09-25T00:00:02Z")], now=T0)
            self.assertTrue(
                (Path(name) / "BTC-USD" / "2026-09-25.jsonl.gz").is_file()
            )

    def test_a_write_error_is_reported_once_per_hour_and_never_raises(self) -> None:
        with tempfile.TemporaryDirectory() as name:
            blocker = Path(name) / "not-a-dir"
            blocker.write_text("x", encoding="utf-8")
            tape = QuoteTape(blocker, flush_seconds=0)
            first = tape.record([_quote("1")], now=T0)
            second = tape.record([_quote("1")], now=T0 + timedelta(minutes=10))
            third = tape.record([_quote("1")], now=T0 + timedelta(minutes=61))
        self.assertIsNotNone(first)
        self.assertIsNone(second)
        self.assertIsNotNone(third)

    def test_quotes_without_a_usable_symbol_are_skipped(self) -> None:
        with tempfile.TemporaryDirectory() as name:
            tape = QuoteTape(Path(name), flush_seconds=0)
            tape.record([{**_quote("1"), "symbol": "../evil"}, {"bid": 1}], now=T0)
            self.assertEqual(list(Path(name).iterdir()), [])


class TapeDaemonTests(unittest.TestCase):
    def test_the_daemon_records_every_polled_quote(self) -> None:
        from agentic_trading.broker import Broker
        from agentic_trading.config import load_config
        from agentic_trading.strategies.fixture import FixtureStrategy
        from tests.fakes import FakeMcpClient
        from tests.test_runtime_daemon import (
            _quote as daemon_quote,
            _run,
            _StubFeed,
            _write_config,
            load_tools,
        )

        with tempfile.TemporaryDirectory() as name:
            tmp = Path(name)
            config = load_config(
                _write_config(tmp, extra=[f'tape_dir = "{tmp / "tape"}"'])
            )
            tools = load_tools()
            _run(
                config,
                Broker(FakeMcpClient(tools), tools),
                FixtureStrategy(),
                _StubFeed([daemon_quote()]),
            )
            rows = _rows(tmp / "tape" / "SPY" / "2026-09-16.jsonl.gz")
        self.assertEqual(rows[0]["symbol"], "SPY")


class TapeIsolationTests(unittest.TestCase):
    """Tests and fixtures must never write into the live tape."""

    def test_a_quote_without_an_observed_time_is_not_filed(self) -> None:
        with tempfile.TemporaryDirectory() as name:
            tape = QuoteTape(Path(name), flush_seconds=0)
            tape.record([{**_quote("1"), "observed_at": None}], now=T0)
            self.assertEqual(list(Path(name).iterdir()), [])

    def test_the_default_tape_lives_beside_the_state_directory(self) -> None:
        from agentic_trading.broker import Broker
        from agentic_trading.config import load_config
        from agentic_trading.runtime import _Loop
        from agentic_trading.strategies.fixture import FixtureStrategy
        from tests.fakes import FakeMcpClient
        from tests.test_runtime_daemon import _write_config, load_tools

        with tempfile.TemporaryDirectory() as name:
            tmp = Path(name)
            config = load_config(_write_config(tmp))
            (tmp / "state").mkdir(parents=True, exist_ok=True)
            tools = load_tools()
            loop = _Loop(config, Broker(FakeMcpClient(tools), tools), FixtureStrategy())
        self.assertEqual(loop.tape.directory, tmp / "tape")


class TapeBufferingTests(unittest.TestCase):
    """A minute of rows per write: one gzip member per poll barely compresses."""

    def test_rows_wait_for_the_flush_interval(self) -> None:
        with tempfile.TemporaryDirectory() as name:
            tape = QuoteTape(Path(name))
            path = Path(name) / "BTC-USD" / "2026-09-24.jsonl.gz"
            tape.record([_quote("1")], now=T0)
            tape.record([_quote("2")], now=T0 + timedelta(seconds=30))
            self.assertFalse(path.exists())
            tape.record([_quote("3")], now=T0 + timedelta(seconds=61))
            self.assertEqual([row["bid"] for row in _rows(path)], ["1", "2", "3"])

    def test_flush_writes_whatever_is_pending(self) -> None:
        with tempfile.TemporaryDirectory() as name:
            tape = QuoteTape(Path(name))
            tape.record([_quote("7")], now=T0)
            self.assertIsNone(tape.flush())
            rows = _rows(Path(name) / "BTC-USD" / "2026-09-24.jsonl.gz")
        self.assertEqual([row["bid"] for row in rows], ["7"])

    def test_a_minute_of_rows_compresses_well(self) -> None:
        with tempfile.TemporaryDirectory() as name:
            tape = QuoteTape(Path(name))
            for step in range(200):
                tape.record(
                    [_quote(str(100 + step / 100))],
                    now=T0 + timedelta(seconds=step * 0.25),
                )
            tape.flush()
            path = Path(name) / "BTC-USD" / "2026-09-24.jsonl.gz"
            raw = sum(len(json.dumps(row)) + 1 for row in _rows(path))
            self.assertLess(path.stat().st_size * 3, raw)
