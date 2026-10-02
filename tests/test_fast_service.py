"""The switchboard runs on the venues bus; its failure never stops the feeds."""

from __future__ import annotations

import asyncio
import json
import tempfile
import unittest
from datetime import timedelta
from pathlib import Path
from types import SimpleNamespace as NS

from agentic_trading.fast.service import FastEngine, fast_problem
from agentic_trading.fast.settings import FastConfig
from agentic_trading.venues.daemon import Backoff, run_venues
from agentic_trading.venues.settings import VenuesConfig
from tests.fast_support import T0, quote
from tests.test_venues_daemon import FakeSource

BTC_ONLY = FastConfig(enabled=True, symbols=("BTC/USD",))


def _source():
    ticks = [quote("BTC/USD", 100, 100.1, T0 + timedelta(seconds=i)) for i in range(3)]
    return FakeSource([("emit_block", ticks)], key="alpaca_crypto", venue="alpaca", always_open=True)


def _engine(tmp: Path) -> FastEngine:
    return FastEngine(BTC_ONLY, state_dir=tmp / "state", journal_dir=tmp / "journal", now=T0)


def _run(tmp: Path, engine: FastEngine) -> dict:
    async def scenario():
        stop = asyncio.Event()
        asyncio.get_running_loop().call_later(0.4, stop.set)
        await run_venues(
            state_dir=tmp / "state", venues_config=VenuesConfig(stream_dir=tmp / "stream"),
            credentials={}, venues={}, sources=[_source()], stop=stop, now=lambda: T0,
            stock_open=lambda t: True, health_every=0.02, account_every=0.05,
            backoff_factory=lambda: Backoff(base=0.01, cap=0.02, jitter=0), rate_delay=0.01,
            stop_grace=0.2, engine=engine, fast_status_every=0.02, fast_save_every=0.05,
        )
    asyncio.run(scenario())
    return json.loads((tmp / "state" / "venues.json").read_text())


def _row(state: dict) -> dict:
    return next(v for v in state["venues"] if v["name"] == "switchboard")


class ServiceTests(unittest.TestCase):
    def test_the_engine_reads_the_bus_and_writes_its_status_and_book(self) -> None:
        with tempfile.TemporaryDirectory() as name:
            tmp = Path(name)
            state = _run(tmp, _engine(tmp))
            fast = json.loads((tmp / "state" / "fast.json").read_text())
            book_saved = (tmp / "state" / "desk" / "switchboard.json").is_file()
        self.assertTrue(fast["enabled"])
        self.assertEqual((fast["failed"], fast["coins"][0]["symbol"]), ("", "BTC/USD"))
        self.assertEqual((_row(state)["mode"], _row(state)["status"]), ("paper", "ok"))
        self.assertTrue(book_saved)

    def test_a_strategy_error_stops_only_the_engine(self) -> None:
        with tempfile.TemporaryDirectory() as name:
            tmp = Path(name)
            engine = _engine(tmp)

            def boom(tick):
                raise RuntimeError("boom in a playbook")

            engine.board.on_tick = boom
            state = _run(tmp, engine)
            recorded = (tmp / "stream" / "alpaca" / "BTCUSD" / "2026-10-05.jsonl.gz").exists()
            journal = (tmp / "journal" / "fast-2026-10-05.jsonl").read_text()
        self.assertEqual(_row(state)["status"], "error")
        self.assertIn("boom in a playbook", _row(state)["last_error"])
        self.assertTrue(recorded)  # the feeds and the recorder kept going
        self.assertIn('"event":"fast_failed"', journal)

    def test_a_corrupt_engine_file_is_journaled_as_a_fresh_start(self) -> None:
        with tempfile.TemporaryDirectory() as name:
            tmp = Path(name)
            (tmp / "state" / "fast").mkdir(parents=True)
            (tmp / "state" / "fast" / "engine.json").write_text("{broken")
            engine = _engine(tmp)
            journal = (tmp / "journal" / "fast-2026-10-05.jsonl").read_text()
        self.assertEqual(engine.recent[0]["event"], "fast_reset")
        self.assertIn("engine.json was unreadable", journal)

    def test_misconfiguration_is_named_before_the_service_starts(self) -> None:
        venues = VenuesConfig()
        crypto = [NS(key="alpaca_crypto")]
        self.assertIn("DOGE/USD", fast_problem(FastConfig(enabled=True, symbols=("BTC/USD", "DOGE/USD")), venues, crypto))
        self.assertIn("Alpaca crypto stream", fast_problem(BTC_ONLY, venues, [NS(key="coinbase")]))
        self.assertEqual(fast_problem(BTC_ONLY, venues, crypto), "")
