"""The symbol scout: does the book follow the market, and safely?

The operator's whitelist is the floor. On top of it the agent may adopt what
the market is actually paying attention to — but only what it can price, trade
and exit, and only if it is not a second copy of a bet the book already holds.
These tests pin those rules from the outside, with the broker and the tape
stubbed so the decision logic is what is under test.
"""

from __future__ import annotations

import json
import random
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path

from agentic_trading import discovery
from agentic_trading.config import load_config
from agentic_trading.history import Bar, save_bars
from agentic_trading.history_sync import bar_stem


def _walk(seed: int, *, drift: float, count: int = 300) -> list[float]:
    rng = random.Random(seed)
    price = 100.0
    closes: list[float] = []
    for _ in range(count):
        price *= 1.0 + drift + rng.gauss(0.0, 0.008)
        closes.append(round(price, 2))
    return closes


def _write_bars(directory: Path, symbol: str, closes: list[float]) -> None:
    directory.mkdir(parents=True, exist_ok=True)
    start = datetime(2019, 1, 1, tzinfo=timezone.utc)
    save_bars(
        directory / f"{bar_stem(symbol)}_day.jsonl",
        [
            Bar(
                symbol=bar_stem(symbol),
                start=start + timedelta(days=index),
                open=Decimal(str(close)),
                high=Decimal(str(close * 1.01)),
                low=Decimal(str(close * 0.99)),
                close=Decimal(str(close)),
                volume=Decimal("1000000"),
            )
            for index, close in enumerate(closes)
        ],
    )


class _StubBroker:
    """Only the read surfaces discovery uses, shaped like the real broker's."""

    def __init__(
        self,
        *,
        lists: dict[str, list[str]] | None = None,
        crypto_pairs: list[str] | None = None,
        bar_dir: Path,
    ) -> None:
        self.lists = lists or {}
        self.crypto_pairs = crypto_pairs or []
        self.bar_dir = bar_dir
        self.calls: list[tuple[str, tuple]] = []

    def get_popular_watchlists(self) -> list[dict]:
        return [
            {"id": name, "display_name": name, "symbols": list(symbols)}
            for name, symbols in self.lists.items()
        ]

    def get_watchlists(self) -> list[dict]:
        return []

    def get_watchlist_items(self, list_id: str) -> list[dict]:
        self.calls.append(("get_watchlist_items", (list_id,)))
        return [
            {"symbol": symbol, "object_type": "instrument"}
            for symbol in self.lists.get(list_id, [])
        ]

    def get_currency_pairs(self, *, limit: int = 200) -> list[dict]:
        return [
            {"symbol": symbol, "tradability": "tradable"}
            for symbol in self.crypto_pairs[:limit]
        ]

    def get_quotes(self, symbols: list[str]) -> dict:
        return {
            "quotes": [
                {
                    "symbol": symbol,
                    "bid_price": "99.99",
                    "ask_price": "100.01",
                }
                for symbol in symbols
            ]
        }

    def get_crypto_quotes(self, symbols: list[str]) -> dict:
        return self.get_quotes(symbols)

    def get_tradability(self, symbols: list[str]) -> dict:
        return {
            "results": [
                {"symbol": symbol, "fractional": True} for symbol in symbols
            ]
        }

    def get_historicals(self, symbols: list[str], **_: object) -> dict:
        """Serve the same bars the file has, the way the broker would."""
        symbol = symbols[0]
        path = self.bar_dir / f"{bar_stem(symbol)}_day.jsonl"
        if not path.is_file():
            raise RuntimeError(f"no history for {symbol}")
        records = [
            json.loads(line)
            for line in path.read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]
        return {"data": {"results": [{"symbol": symbol, "bars": records}]}}


def _config(tmp: Path) -> Path:
    path = tmp / "agentic.toml"
    path.write_text(
        "\n".join(
            [
                'mode = "shadow"',
                'strategy = "trend_crypto"',
                'symbol_whitelist = ["SPY"]',
                'max_order_pct = "0.01"',
                'daily_notional_pct = "0.04"',
                'daily_loss_pct = "0.03"',
                "max_open_positions = 4",
                "equity_refresh_ticks = 30",
                "equity_refresh_seconds = 60",
                'timezone = "local"',
                f'quotes_path = "{tmp / "quotes.jsonl"}"',
                f'journal_dir = "{tmp / "journal"}"',
                f'state_dir = "{tmp / "state"}"',
                f'history_path = "{tmp / "bars"}"',
                f'tools_snapshot_path = "{tmp / "tools.json"}"',
                f'token_path = "{tmp / "missing.json"}"',
                'mcp_url = "https://agent.robinhood.com/mcp/trading"',
                "discovery_enabled = true",
                "discovery_max_symbols = 2",
                "discovery_min_bars = 260",
                'discovery_min_dollar_volume = "1000000"',
                "discovery_min_hold_hours = 0",
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    return path


class SymbolScoutTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        self.bars = self.tmp / "bars"
        # SPY is the operator's core; it is always in the book.
        _write_bars(self.bars, "SPY", _walk(1, drift=0.004))

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def _config(self):
        return load_config(_config(self.tmp))

    def test_a_trending_name_the_operator_did_not_list_is_adopted(self) -> None:
        _write_bars(self.bars, "NVDA", _walk(2, drift=0.006))
        broker = _StubBroker(lists={"Trending stocks": ["NVDA"]}, bar_dir=self.bars)
        config = self._config()

        report = discovery.rebalance(config, broker, held=())

        self.assertEqual(report["added"], ["NVDA"])
        self.assertEqual(report["adopted"], ["NVDA"])
        # The adopted symbol is tradeable: the effective whitelist the guard
        # and the strategy read includes it without editing the operator's list.
        reloaded = load_config(_config(self.tmp))
        self.assertIn("NVDA", reloaded.effective_whitelist)
        self.assertEqual(reloaded.symbol_whitelist, frozenset({"SPY"}))

    def test_a_candidate_without_history_is_not_adopted(self) -> None:
        _write_bars(self.bars, "AAPL", _walk(3, drift=0.006, count=100))
        broker = _StubBroker(lists={"Trending stocks": ["AAPL"]}, bar_dir=self.bars)

        report = discovery.rebalance(self._config(), broker, held=())

        self.assertEqual(report["added"], [])
        rows = {row["symbol"]: row for row in report["candidates"]}
        self.assertEqual(rows["AAPL"]["code"], "needs_history")
        self.assertIn("260", rows["AAPL"]["reason"])

    def test_an_adopted_symbol_whose_trend_breaks_is_dropped_when_flat(self) -> None:
        _write_bars(self.bars, "NVDA", _walk(2, drift=0.006))
        broker = _StubBroker(lists={"Trending stocks": ["NVDA"]}, bar_dir=self.bars)
        config = self._config()
        discovery.rebalance(config, broker, held=())

        # The tape turns. Nothing holds it, so it leaves the book.
        _write_bars(self.bars, "NVDA", _walk(2, drift=-0.004))
        report = discovery.rebalance(
            load_config(_config(self.tmp)), broker, held=()
        )

        self.assertEqual(report["dropped"], ["NVDA"])
        self.assertEqual(report["adopted"], [])

    def test_a_held_symbol_is_kept_even_after_its_trend_breaks(self) -> None:
        """Dropping a held symbol would strand the exit; the scout waits."""
        _write_bars(self.bars, "NVDA", _walk(2, drift=0.006))
        broker = _StubBroker(lists={"Trending stocks": ["NVDA"]}, bar_dir=self.bars)
        config = self._config()
        discovery.rebalance(config, broker, held=())

        _write_bars(self.bars, "NVDA", _walk(2, drift=-0.004))
        report = discovery.rebalance(
            load_config(_config(self.tmp)), broker, held=("NVDA",)
        )

        self.assertEqual(report["held_back"], ["NVDA"])
        self.assertEqual(report["adopted"], ["NVDA"])
        reloaded = load_config(_config(self.tmp))
        self.assertIn("NVDA", reloaded.effective_whitelist)

    def test_a_second_copy_of_an_existing_bet_is_refused(self) -> None:
        # QQQ is priced off the same walk as SPY: one bet wearing two tickers.
        closes = _walk(1, drift=0.004)
        _write_bars(self.bars, "QQQ", closes)
        broker = _StubBroker(lists={"Trending stocks": ["QQQ"]}, bar_dir=self.bars)

        report = discovery.rebalance(self._config(), broker, held=())

        self.assertEqual(report["added"], [])
        rows = {row["symbol"]: row for row in report["candidates"]}
        self.assertEqual(rows["QQQ"]["code"], "duplicate_bet")
        self.assertIn("SPY", rows["QQQ"]["reason"])

    def test_a_scout_that_cannot_see_the_market_does_not_widen_the_book(self) -> None:
        _write_bars(self.bars, "NVDA", _walk(2, drift=0.006))
        broker = _StubBroker(lists={}, bar_dir=self.bars)  # every list surface unavailable

        report = discovery.rebalance(self._config(), broker, held=())

        self.assertEqual(report["added"], [])
        self.assertEqual(report["adopted"], [])
        # A scout that cannot see the market says so; it does not guess.
        self.assertTrue(report["notes"])

    def test_a_symbol_that_is_not_a_ticker_is_ignored(self) -> None:
        """A broker payload must not be able to name a path or a URL.

        Watchlist items become file names (``<SYMBOL>_day.jsonl``) and Coinbase
        URLs, so a symbol like ``../../ETC/PASSWD`` is a write primitive, not a
        ticker. It is skipped at ingest.
        """
        broker = _StubBroker(
            lists={"Trending stocks": ["../../ETC/PASSWD", "NVDA?x=1"]},
            bar_dir=self.bars,
        )

        report = discovery.rebalance(self._config(), broker, held=())

        self.assertEqual(report["added"], [])
        self.assertEqual(report["adopted"], [])
        # The traversal target was never created next to the bar directory.
        self.assertFalse((self.tmp / "ETC").exists())
        self.assertFalse((self.tmp.parent / "ETC").exists())

    def test_the_path_builder_refuses_a_name_that_is_not_a_ticker(self) -> None:
        with self.assertRaises(ValueError):
            discovery.bars_path(self.bars, "../../ETC/PASSWD")
        with self.assertRaises(ValueError):
            discovery.bars_path(self.bars, "BTC?x=1")

    def test_the_crypto_fetch_refuses_a_name_that_is_not_a_ticker(self) -> None:
        from agentic_trading.history_sync import fetch_crypto_records

        # Raised before any HTTP call: httpx is imported but never reached.
        for hostile in ("../../ETC/PASSWD", "BTC?x=1", "BTC&x=1", ""):
            with self.assertRaises(ValueError):
                fetch_crypto_records(hostile)


if __name__ == "__main__":
    unittest.main()
