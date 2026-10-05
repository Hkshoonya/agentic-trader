"""Replay runs the live code: same ticks, same decisions; bars are labelled approximate."""

from __future__ import annotations

import contextlib
import gzip
import io
import json
import tempfile
import unittest
from datetime import date, timedelta
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace as NS

from agentic_trading.fast.cli import cmd_replay, status_lines
from agentic_trading.fast.replay import bar_ticks, fetch_bars, recorded_ticks, replay
from agentic_trading.fast.service import FastEngine
from agentic_trading.fast.settings import FastConfig
from tests.fast_support import T0, quote
from tests.test_runtime_daemon import _write_config

CFG = FastConfig(enabled=True, symbols=("BTC/USD",))
DAY = T0.date()


def _market() -> list:
    """150 minutes: a steady climb (1.5/min) that trips a breakout, then a slide that stops it out."""
    ticks = []
    for minute in range(150):
        base = 20 + 1.5 * minute if minute < 100 else 170 - 3 * (minute - 100)
        for second, wiggle in ((0, 0.0), (15, 0.3), (30, -0.2), (45, 0.1)):
            at = T0 + timedelta(minutes=minute, seconds=second)
            price = base + wiggle
            ticks.append(quote("BTC-USD", round(price - 0.1, 2), round(price + 0.1, 2), at, venue="coinbase"))
            ticks.append(quote("BTC/USD", round(price - 0.05, 2), round(price + 0.05, 2), at))
    return ticks


def _record(stream: Path, ticks: list) -> None:
    for venue in ("alpaca", "coinbase"):
        folder = stream / venue / "BTCUSD"
        folder.mkdir(parents=True)
        with gzip.open(folder / f"{DAY.isoformat()}.jsonl.gz", "wt", encoding="utf-8") as handle:
            for tick in ticks:
                if tick.venue == venue:
                    handle.write(json.dumps(tick.to_row()) + "\n")


def _seen(events) -> list:
    return [(e.kind, e.at.isoformat(), e.text) for e in events]


class ReplayTests(unittest.TestCase):
    def test_replay_is_deterministic_and_matches_the_live_engine(self) -> None:
        with tempfile.TemporaryDirectory() as name:
            tmp = Path(name)
            _record(tmp / "stream", _market())
            first = replay(recorded_ticks(tmp / "stream", CFG.symbols, DAY, DAY), CFG, label="exact")
            second = replay(recorded_ticks(tmp / "stream", CFG.symbols, DAY, DAY), CFG, label="exact")
            engine = FastEngine(CFG, state_dir=tmp / "state", journal_dir=tmp / "journal", now=T0)
            for tick in recorded_ticks(tmp / "stream", CFG.symbols, DAY, DAY):
                engine.on_tick(tick)
            live = [json.loads(line) for line in (tmp / "journal" / f"fast-{DAY.isoformat()}.jsonl").read_text().splitlines()]
        kinds = [e.kind for e in first.events]
        self.assertIn("fast_entry", kinds)
        self.assertIn("fast_exit", kinds)
        self.assertEqual(_seen(first.events), _seen(second.events))
        self.assertEqual([(r["event"], r["at"], r["text"]) for r in live], _seen(first.events))
        self.assertEqual(first.ticks, 1200)
        self.assertIn("breakout", first.playbooks)
        self.assertIsNotNone(first.coinbase_return_pct)

    def test_bars_become_four_prices_with_coinbase_first(self) -> None:
        rows = {"BTC/USD": [{"t": T0.isoformat(), "o": "10", "h": "12", "l": "9", "c": "11"},
                            {"t": (T0 + timedelta(minutes=1)).isoformat(), "o": "11", "h": "13", "l": "8", "c": "9"}]}
        ticks = list(bar_ticks(rows, Decimal("0")))
        self.assertEqual([(t.venue, t.symbol) for t in ticks[:2]], [("coinbase", "BTC-USD"), ("alpaca", "BTC/USD")])
        alpaca = [t for t in ticks if t.venue == "alpaca"]
        self.assertEqual([float(t.bid) for t in alpaca], [10, 9, 12, 11, 11, 13, 8, 9])  # up bar: low first
        self.assertEqual(alpaca[1].received_at - alpaca[0].received_at, timedelta(seconds=15))

    def test_bars_are_cached_per_day_and_fetched_once(self) -> None:
        calls = []

        def fake(symbol, start, end):
            calls.append((symbol, start, end))
            return [NS(timestamp=T0 + timedelta(days=d, minutes=m), open=1, high=2, low=0.5, close=1.5)
                    for d in range(2) for m in range(3)]

        with tempfile.TemporaryDirectory() as name:
            first = fetch_bars(("BTC/USD",), DAY, DAY + timedelta(days=1), name, fetch=fake)
            again = fetch_bars(("BTC/USD",), DAY, DAY + timedelta(days=1), name, fetch=fake)
        self.assertEqual(len(calls), 1)
        self.assertEqual(len(first["BTC/USD"]), 6)
        self.assertEqual(first, again)


class CommandTests(unittest.TestCase):
    def _config(self, tmp: Path) -> Path:
        path = _write_config(tmp)
        path.write_text(path.read_text() + f'\n[venues]\nstream_dir = "{tmp / "stream"}"\n'
                        '\n[fast]\nsymbols = ["BTC/USD"]\n')
        return path

    def test_bar_replay_says_approximate_and_skips_unfinished_days(self) -> None:
        def fake(symbol, start, end):
            return [NS(timestamp=T0 + timedelta(minutes=m), open=20 + 1.5 * m, high=20.3 + 1.5 * m,
                       low=19.8 + 1.5 * m, close=20.1 + 1.5 * m) for m in range(120)]

        with tempfile.TemporaryDirectory() as name:
            path = self._config(Path(name))
            out = io.StringIO()
            with contextlib.redirect_stdout(out):
                code = cmd_replay(str(path), "bars", DAY.isoformat(), (DAY + timedelta(days=3)).isoformat(),
                                  fetch=fake, today=DAY + timedelta(days=1))
        self.assertEqual(code, 0)
        self.assertIn("approximate", out.getvalue())
        self.assertIn("spread (the median of recordings so far)", out.getvalue())
        self.assertIn(f"{DAY.isoformat()} → {DAY.isoformat()}", out.getvalue())  # clamped to finished days

    def test_a_replay_can_try_another_bar_length_without_editing_the_config(self) -> None:
        def fake(symbol, start, end):
            return [NS(timestamp=T0 + timedelta(minutes=m), open=20.0, high=20.1, low=19.9, close=20.0)
                    for m in range(30)]

        with tempfile.TemporaryDirectory() as name:
            path = self._config(Path(name))
            out = io.StringIO()
            with contextlib.redirect_stdout(out):
                code = cmd_replay(str(path), "bars", DAY.isoformat(), DAY.isoformat(),
                                  fetch=fake, today=DAY + timedelta(days=1), bar_minutes=5)
            self.assertNotIn("bar_minutes", path.read_text())
        self.assertEqual(code, 0)
        self.assertIn("trading 5-minute bars", out.getvalue())

    def test_recorded_replay_with_nothing_recorded_says_so(self) -> None:
        with tempfile.TemporaryDirectory() as name:
            path = self._config(Path(name))
            out = io.StringIO()
            with contextlib.redirect_stdout(out):
                code = cmd_replay(str(path), "recorded", DAY.isoformat(), DAY.isoformat())
        self.assertEqual(code, 1)
        self.assertIn("no prices", out.getvalue())

    def test_status_reads_in_plain_words(self) -> None:
        lines = status_lines({
            "as_of": T0.isoformat(), "failed": "", "book": {"equity": "50.10", "return_pct": 0.2},
            "mirror": {"return_pct": -0.4},
            "coins": [{"symbol": "BTC/USD", "regime": "trending", "standing_aside": False,
                       "trade": {"playbook": "breakout", "entry": 105.2, "stop": 99.2, "pnl_pct": 0.5}},
                      {"symbol": "ETH/USD", "regime": "choppy", "standing_aside": True, "trade": None}],
            "recent": [{"at": T0.isoformat(), "text": "Bought $33.00 of BTC/USD"}],
        })
        text = "\n".join(lines)
        self.assertIn("breakout open at 105.2", text)
        self.assertIn("ETH/USD: choppy · standing aside", text)
        self.assertIn("-0.40% at Coinbase costs", text)
