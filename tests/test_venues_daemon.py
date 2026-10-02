"""The venues daemon, end to end on fakes: ticks flow, errors are handled."""

from __future__ import annotations

import asyncio
import gzip
import json
import tempfile
import threading
import unittest
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path

from agentic_trading.venues.daemon import (
    Backoff, build_sources, build_venues, classify_error, run_venues,
)
from agentic_trading.venues.journal import VenueJournal
from agentic_trading.venues.model import AccountView, Tick
from agentic_trading.venues.secrets import Credentials
from agentic_trading.venues.settings import VenuesConfig

T0 = datetime(2026, 9, 30, 15, 0, tzinfo=timezone.utc)
SECRET = "papersecret-DO-NOT-LEAK-1234567890"
CREDS = {"alpaca_paper": Credentials("alpaca_paper", "PKTESTKEY0000000WXYZ", SECRET)}


class FakeSource:
    """Each run() follows the next script step: a list of ticks to emit then
    return, ("emit_block", ticks) to emit and stay connected, an exception to
    raise, or "block" (stay connected until stop())."""

    def __init__(self, steps, *, key="alpaca_stocks", venue="alpaca", always_open=False):
        self.steps, self.key, self.venue, self.always_open = list(steps), key, venue, always_open
        self.symbols = ("SPY",)
        self.runs = 0
        self._stopped = threading.Event()

    def run(self, emit):
        self.runs += 1
        step = self.steps.pop(0) if self.steps else "block"
        if isinstance(step, BaseException):
            raise step
        if step == "block":
            self._stopped.wait(5)
            return
        if isinstance(step, tuple) and step[0] == "emit_block":
            for tick in step[1]:
                emit(tick)
            self._stopped.wait(5)
            return
        for tick in step:
            emit(tick)

    def stop(self):
        self._stopped.set()



class StubbornSource(FakeSource):
    """Like the real SDKs at their worst: stop() raises (Alpaca before its loop
    exists) and run() never returns (Coinbase after a clean close)."""

    def run(self, emit):
        self.runs += 1
        for tick in self.steps.pop(0) if self.steps else []:
            emit(tick)
        threading.Event().wait(30)

    def stop(self):
        raise AttributeError("'NoneType' object has no attribute 'is_running'")


class FakeVenue:
    def __init__(self, name="alpaca_paper", error=None):
        self.name, self.mode, self.error = name, "paper", error

    def account(self):
        if self.error:
            raise self.error
        return AccountView(self.name, "paper", Decimal("100000"), Decimal("100000"), Decimal("200000"))


def _tick(i=0):
    at = T0 + timedelta(seconds=i)
    return Tick("alpaca", "SPY", Decimal("500"), Decimal("500.02"), None, None, at, at + timedelta(milliseconds=30))


def _run(tmp: Path, sources, venues=None, *, seconds=0.4, now=None, stock_open=lambda t: True, stop_grace=1.0):
    async def scenario():
        stop = asyncio.Event()
        asyncio.get_running_loop().call_later(seconds, stop.set)
        await run_venues(
            state_dir=tmp / "state", venues_config=VenuesConfig(stream_dir=tmp / "stream"),
            credentials=CREDS, venues=venues or {}, sources=sources, stop=stop,
            now=now or (lambda: T0), stock_open=stock_open, health_every=0.02, account_every=0.05,
            backoff_factory=lambda: Backoff(base=0.01, cap=0.02, jitter=0), rate_delay=0.01,
            stop_grace=stop_grace,
        )
    asyncio.run(scenario())
    return json.loads((tmp / "state" / "venues.json").read_text())


class DaemonTests(unittest.TestCase):
    def test_ticks_reach_the_recorder_the_latest_table_and_health(self) -> None:
        with tempfile.TemporaryDirectory() as name:
            tmp = Path(name)
            state = _run(tmp, [FakeSource([[_tick(0), _tick(1), _tick(2)], "block"])])
            path = tmp / "stream" / "alpaca" / "SPY" / "2026-09-30.jsonl.gz"
            with gzip.open(path, "rt") as handle:
                rows = handle.read().splitlines()
        self.assertEqual(len(rows), 3)
        self.assertEqual(state["latest"]["alpaca:SPY"]["bid"], "500")
        self.assertEqual(state["streams"][0]["status"], "off")  # written on shutdown

    def test_shutdown_finishes_even_when_a_source_ignores_stop(self) -> None:
        import time

        with tempfile.TemporaryDirectory() as name:
            tmp = Path(name)
            started = time.monotonic()
            state = _run(tmp, [StubbornSource([[_tick(0)]]), FakeSource(["block"], key="coinbase", venue="coinbase")],
                         stop_grace=0.2)
            took = time.monotonic() - started
            path = tmp / "stream" / "alpaca" / "SPY" / "2026-09-30.jsonl.gz"
            self.assertTrue(path.exists())  # the recorder was flushed on the way out
        self.assertLess(took, 5)
        self.assertEqual({s["status"] for s in state["streams"]}, {"off"})

    def test_an_auth_failure_is_not_retried(self) -> None:
        with tempfile.TemporaryDirectory() as name:
            source = FakeSource([RuntimeError("401 Unauthorized")])
            state = _run(Path(name), [source])
        self.assertEqual(source.runs, 1)
        self.assertEqual(state["streams"][0]["status"], "auth_failed")

    def test_other_errors_reconnect_with_backoff(self) -> None:
        with tempfile.TemporaryDirectory() as name:
            source = FakeSource([RuntimeError("boom"), RuntimeError("boom"), "block"])
            state = _run(Path(name), [source])
        self.assertEqual(source.runs, 3)
        self.assertEqual(state["streams"][0]["reconnects"], 2)

    def test_a_secret_in_an_error_never_reaches_the_state_file(self) -> None:
        with tempfile.TemporaryDirectory() as name:
            _run(Path(name), [FakeSource([RuntimeError(f"bad secret {SECRET}"), "block"])])
            text = (Path(name) / "state" / "venues.json").read_text()
        self.assertNotIn(SECRET, text)
        self.assertIn("[redacted]", text)

    def test_stock_streams_are_not_stale_when_the_market_is_closed(self) -> None:
        clock = {"now": T0}

        def later():
            clock["now"] += timedelta(seconds=10)
            return clock["now"]

        for market_open, expected in ((False, "closed"), (True, "stale")):
            with self.subTest(market_open=market_open), tempfile.TemporaryDirectory() as name:
                clock["now"] = T0
                seen = {}

                async def scenario(tmp=Path(name)):
                    stop = asyncio.Event()

                    async def snoop():
                        await asyncio.sleep(0.3)
                        seen["state"] = json.loads((tmp / "state" / "venues.json").read_text())
                        stop.set()

                    await asyncio.gather(
                        run_venues(state_dir=tmp / "state", venues_config=VenuesConfig(stream_dir=tmp / "s"),
                                   credentials=CREDS, venues={}, sources=[FakeSource([("emit_block", [_tick(0)])])],
                                   stop=stop, now=later, stock_open=lambda t, o=market_open: o,
                                   health_every=0.02, account_every=1, backoff_factory=Backoff),
                        snoop(),
                    )

                asyncio.run(scenario())
                self.assertEqual(seen["state"]["streams"][0]["status"], expected)

    def test_accounts_are_polled_and_an_auth_failure_is_named(self) -> None:
        with tempfile.TemporaryDirectory() as name:
            state = _run(Path(name), [], {"alpaca_paper": FakeVenue(),
                                          "alpaca_live": FakeVenue("alpaca_live", RuntimeError("403 forbidden"))})
        venues = {v["name"]: v for v in state["venues"]}
        self.assertEqual((venues["alpaca_paper"]["status"], venues["alpaca_paper"]["equity"]), ("ok", "100000"))
        self.assertEqual(venues["alpaca_live"]["status"], "auth_failed")


class PartsTests(unittest.TestCase):
    def test_classify_error(self) -> None:
        self.assertEqual(classify_error(RuntimeError("HTTP 401 Unauthorized")), "auth")
        self.assertEqual(classify_error(PermissionError("forbidden")), "auth")
        self.assertEqual(classify_error(RuntimeError("429 Too Many Requests")), "rate")
        self.assertEqual(classify_error(RuntimeError("connection limit exceeded")), "other")

    def test_backoff_doubles_caps_and_jitters(self) -> None:
        backoff = Backoff(base=1, cap=4, jitter=0.2, rand=lambda: 1.0)
        self.assertEqual([round(backoff.next(), 2) for _ in range(4)], [1.2, 2.4, 4.8, 4.8])
        backoff.reset()
        self.assertEqual(Backoff(base=1, cap=4, jitter=0.2, rand=lambda: 0.0).next(), 0.8)

    def test_only_enabled_venues_with_keys_are_built(self) -> None:
        both = {**CREDS, "coinbase": Credentials("coinbase", "organizations/o/apiKeys/k1234", "x" * 20)}
        factories = {"alpaca_trading": lambda c, p: object(), "coinbase_rest": lambda c: object()}
        venues = build_venues(VenuesConfig(use_coinbase=True), both, factories=factories)
        self.assertEqual(sorted(venues), ["alpaca_paper", "coinbase"])
        self.assertEqual(sorted(build_venues(VenuesConfig(), {}, factories=factories)), [])
        keys = [s.key for s in build_sources(VenuesConfig(use_coinbase=True), both)]
        self.assertEqual(keys, ["alpaca_stocks", "alpaca_crypto", "coinbase"])
        self.assertEqual([s.key for s in build_sources(VenuesConfig(), {})], [])


class JournalTests(unittest.TestCase):
    def test_journal_lines_are_dated_and_redacted(self) -> None:
        with tempfile.TemporaryDirectory() as name:
            VenueJournal(name, CREDS.values()).append({"event": "venue_refused", "error": SECRET}, now=T0)
            text = (Path(name) / "venues-2026-09-30.jsonl").read_text()
        record = json.loads(text)
        self.assertEqual((record["event"], record["error"]), ("venue_refused", "[redacted]"))
        self.assertEqual(record["at"], T0.isoformat())
