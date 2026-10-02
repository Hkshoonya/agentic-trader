"""Ticks fan out, are recorded, and each stream's health is measured."""

from __future__ import annotations

import asyncio
import gzip
import json
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path

from agentic_trading.venues.bus import TickBus
from agentic_trading.venues.health import HealthBoard, StreamHealth
from agentic_trading.venues.model import Tick
from agentic_trading.venues.recorder import StreamRecorder

T0 = datetime(2026, 9, 30, 15, 0, tzinfo=timezone.utc)


def _tick(symbol="SPY", venue="alpaca_paper", at=T0, delay_ms=40.0, bid="500"):
    return Tick(venue, symbol, Decimal(bid), Decimal(bid) + Decimal("0.02"), None, None,
                at, at + timedelta(milliseconds=delay_ms))


class BusTests(unittest.TestCase):
    def test_every_subscriber_gets_every_tick_and_a_slow_one_drops_its_oldest(self) -> None:
        async def scenario():
            bus = TickBus()
            fast, slow = bus.subscribe(), bus.subscribe(maxsize=2)
            for i in range(3):
                bus.publish(_tick(bid=str(500 + i)))
            return fast.qsize(), [slow.get_nowait().bid for _ in range(2)], bus.dropped

        fast, slow, dropped = asyncio.run(scenario())
        self.assertEqual(fast, 3)
        self.assertEqual(slow, [Decimal("501"), Decimal("502")])
        self.assertEqual(dropped, 1)


class RecorderTests(unittest.TestCase):
    def test_ticks_are_buffered_then_written_per_venue_symbol_and_day(self) -> None:
        with tempfile.TemporaryDirectory() as name:
            recorder = StreamRecorder(name, flush_seconds=60)
            recorder.record(_tick("BTC/USD", "alpaca_paper"), now=T0)
            path = Path(name) / "alpaca_paper" / "BTCUSD" / "2026-09-30.jsonl.gz"
            self.assertFalse(path.exists())
            recorder.record(_tick("BTC/USD", "alpaca_paper"), now=T0 + timedelta(seconds=61))
            with gzip.open(path, "rt", encoding="utf-8") as handle:
                rows = [json.loads(line) for line in handle]
        self.assertEqual(len(rows), 2)
        self.assertEqual(rows[0]["symbol"], "BTC/USD")
        self.assertEqual(rows[0]["bid"], "500")

    def test_a_disk_error_is_reported_once_an_hour_and_rows_are_dropped(self) -> None:
        with tempfile.TemporaryDirectory() as name:
            blocker = Path(name) / "alpaca_paper"
            blocker.write_text("a file where a directory must go")
            recorder = StreamRecorder(name, flush_seconds=0)
            first = recorder.record(_tick(), now=T0)
            second = recorder.record(_tick(), now=T0 + timedelta(minutes=5))
        self.assertTrue(first)
        self.assertIsNone(second)


class HealthTests(unittest.TestCase):
    def test_delay_median_and_p95_over_the_last_minute(self) -> None:
        stream = StreamHealth("alpaca_stocks", "alpaca_paper", always_open=False)
        stream.on_tick(_tick(at=T0 - timedelta(minutes=5), delay_ms=5000))  # too old to count
        for ms in (10, 20, 30, 40, 1000):
            stream.on_tick(_tick(delay_ms=ms))
        median, p95 = stream.delay_ms(T0 + timedelta(seconds=1))
        self.assertEqual(median, 30)
        self.assertEqual(p95, 40)
        self.assertEqual(stream.status, "live")

    def test_clock_skew_clamps_to_zero_and_is_counted(self) -> None:
        stream = StreamHealth("coinbase", "coinbase", always_open=True)
        stream.on_tick(_tick(delay_ms=-250))
        median, _ = stream.delay_ms(T0 + timedelta(seconds=1))
        self.assertEqual(median, 0)
        self.assertEqual(stream.skew, 1)

    def test_stale_only_while_the_market_is_open(self) -> None:
        stream = StreamHealth("alpaca_stocks", "alpaca_paper", always_open=False)
        stream.on_tick(_tick())
        later = T0 + timedelta(seconds=90)
        stream.evaluate(later, stale_after=30, market_open=False)
        self.assertEqual(stream.status, "closed")
        stream.evaluate(later, stale_after=30, market_open=True)
        self.assertEqual(stream.status, "stale")
        stream.on_tick(_tick(at=later))
        self.assertEqual(stream.status, "live")

    def test_a_stream_that_never_ticks_goes_stale_after_it_starts(self) -> None:
        stream = StreamHealth("alpaca_stocks", "alpaca_paper", always_open=False)
        stream.begin(T0)
        stream.evaluate(T0 + timedelta(seconds=10), stale_after=30, market_open=True)
        self.assertEqual(stream.status, "starting")
        stream.evaluate(T0 + timedelta(seconds=40), stale_after=30, market_open=True)
        self.assertEqual(stream.status, "stale")
        stream.evaluate(T0 + timedelta(seconds=40), stale_after=30, market_open=False)
        self.assertEqual(stream.status, "closed")

    def test_auth_failure_is_sticky(self) -> None:
        stream = StreamHealth("coinbase", "coinbase", always_open=True)
        stream.status = "auth_failed"
        stream.evaluate(T0, stale_after=30, market_open=True)
        self.assertEqual(stream.status, "auth_failed")

    def test_the_board_tracks_latest_prices_and_writes_json(self) -> None:
        board = HealthBoard()
        board.stream("alpaca_stocks", "alpaca_paper", always_open=False)
        board.on_tick("alpaca_stocks", _tick(bid="501"))
        board.set_venue("alpaca_paper", mode="paper", status="ok", equity="100000")
        with tempfile.TemporaryDirectory() as name:
            path = Path(name) / "venues.json"
            board.write(path, T0 + timedelta(seconds=1))
            data = json.loads(path.read_text())
        self.assertEqual(data["latest"]["alpaca_paper:SPY"]["bid"], "501")
        self.assertEqual(data["streams"][0]["status"], "live")
        self.assertEqual(data["venues"][0]["name"], "alpaca_paper")


class ServiceUnitTests(unittest.TestCase):
    def test_the_unit_neither_loops_when_off_nor_hangs_on_stop(self) -> None:
        unit = (Path(__file__).resolve().parents[1] / "deploy" / "agentic-trading-venues.service").read_text()
        self.assertIn("Restart=on-failure", unit)  # disabled (exit 0) stays stopped
        self.assertIn("RestartPreventExitStatus=2", unit)  # missing keys: no 10 s loop
        self.assertIn("TimeoutStopSec=", unit)
