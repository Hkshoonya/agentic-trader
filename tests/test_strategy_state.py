"""The trend strategy's position book must match reality and survive restarts."""

from __future__ import annotations

import json
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path

from agentic_trading.strategies.trend_crypto import TrendCryptoStrategy


def _write_bars(directory: Path, symbol: str, closes: list[float]) -> None:
    rows = []
    start = datetime(2025, 1, 1, tzinfo=timezone.utc)
    for index, close in enumerate(closes):
        rows.append(
            json.dumps(
                {
                    "symbol": symbol,
                    "start": (start + timedelta(days=index)).isoformat(),
                    "open": str(close),
                    "high": str(close),
                    "low": str(close),
                    "close": str(close),
                    "volume": "10",
                }
            )
        )
    (directory / f"{symbol.replace('-', '')}_day.jsonl").write_text(
        "\n".join(rows) + "\n"
    )


def _quote(symbol: str, price: float, when: datetime) -> dict:
    return {
        "symbol": symbol.replace("-", ""),
        "observed_at": when.isoformat(),
        "bid": Decimal(str(price * 0.999)),
        "ask": Decimal(str(price * 1.001)),
    }


class PositionBookTests(unittest.TestCase):
    def _strategy(self, tmp: Path, *, state_path: Path | None = None) -> TrendCryptoStrategy:
        bars = tmp / "bars"
        bars.mkdir(exist_ok=True)
        _write_bars(bars, "BTC-USD", [100.0 + i for i in range(300)])
        _write_bars(bars, "ETH-USD", [200.0 - i * 0.2 for i in range(300)])
        return TrendCryptoStrategy(
            bar_dir=bars,
            symbols=["BTC-USD", "ETH-USD"],
            state_path=state_path,
        )

    def test_a_fill_is_found_under_the_bar_key(self) -> None:
        """The runtime reports BTC-USD; the strategy reasons in BTCUSD."""
        with tempfile.TemporaryDirectory() as name:
            strategy = self._strategy(Path(name))
            strategy.note_fill("BTC-USD", Decimal("0.0015"))
            self.assertEqual(strategy._held_quantity("BTCUSD"), Decimal("0.0015"))
            self.assertEqual(strategy._held, {"BTCUSD"})

    def test_a_sell_reduces_the_book(self) -> None:
        with tempfile.TemporaryDirectory() as name:
            strategy = self._strategy(Path(name))
            strategy.note_fill("BTC-USD", Decimal("0.002"))
            strategy.note_fill("BTC-USD", Decimal("-0.002"))
            self.assertEqual(strategy._held, set())

    def test_an_over_sell_does_not_leave_a_negative_position(self) -> None:
        with tempfile.TemporaryDirectory() as name:
            strategy = self._strategy(Path(name))
            strategy.note_fill("BTC-USD", Decimal("0.001"))
            strategy.note_fill("BTC-USD", Decimal("-0.005"))
            self.assertEqual(strategy._held, set())
            self.assertEqual(strategy._held_quantity("BTC-USD"), Decimal("0"))

    def test_emitting_an_intent_does_not_create_a_position(self) -> None:
        """A vetoed entry must not leave the strategy thinking it is long."""
        with tempfile.TemporaryDirectory() as name:
            strategy = self._strategy(Path(name))
            when = datetime.now(timezone.utc)
            intents = strategy.on_quote(_quote("BTC-USD", 400.0, when))
            self.assertTrue(intents, "expected an entry intent")
            self.assertEqual(strategy._held, set())


class RestartTests(PositionBookTests):
    def test_holdings_and_the_daily_guard_survive_a_restart(self) -> None:
        with tempfile.TemporaryDirectory() as name:
            tmp = Path(name)
            state = tmp / "strategy.json"
            first = self._strategy(tmp, state_path=state)
            first.note_fill("BTC-USD", Decimal("0.0015"))
            when = datetime.now(timezone.utc)
            first.on_quote(_quote("BTC-USD", 400.0, when))
            self.assertNotEqual(first._last_decision_date, "")

            restarted = self._strategy(tmp, state_path=state)

        self.assertEqual(restarted._held_quantity("BTCUSD"), Decimal("0.0015"))
        self.assertEqual(restarted._last_decision_date, first._last_decision_date)
        # The same day must not rebalance twice just because the process died.
        self.assertEqual(restarted.on_quote(_quote("BTC-USD", 400.0, when)), [])

    def test_seeding_replaces_a_stale_book_with_the_runtime_view(self) -> None:
        with tempfile.TemporaryDirectory() as name:
            tmp = Path(name)
            state = tmp / "strategy.json"
            strategy = self._strategy(tmp, state_path=state)
            strategy.note_fill("BTC-USD", Decimal("0.0015"))  # phantom entry

            seeded = strategy.seed_positions({"ETH-USD": "0.02"})

            self.assertEqual(seeded, 1)
            self.assertEqual(strategy._held, {"ETHUSD"})
            self.assertEqual(strategy._held_quantity("BTCUSD"), Decimal("0"))

    def test_a_corrupt_state_file_does_not_break_startup(self) -> None:
        with tempfile.TemporaryDirectory() as name:
            tmp = Path(name)
            state = tmp / "strategy.json"
            state.write_text("{not json")
            strategy = self._strategy(tmp, state_path=state)
            self.assertEqual(strategy._held, set())
            self.assertEqual(strategy._last_decision_date, "")

    def test_the_strategy_can_exit_a_seeded_position(self) -> None:
        """The bug this pins: exits were impossible because quantity read 0."""
        with tempfile.TemporaryDirectory() as name:
            strategy = self._strategy(Path(name))
            # ETH is the downtrend in the fixture, so it is not a target and
            # must be exited — which needs its quantity to be readable.
            strategy.seed_positions({"ETH-USD": "0.02"})
            when = datetime.now(timezone.utc) + timedelta(days=1)
            intents = strategy.on_quote(_quote("BTC-USD", 400.0, when))
            exits = [i for i in intents if i.side.value == "sell"]
            self.assertTrue(exits, [i.symbol + ":" + i.side.value for i in intents])
            self.assertEqual(exits[0].symbol, "ETH-USD")
            self.assertEqual(exits[0].quantity, Decimal("0.02"))


if __name__ == "__main__":
    unittest.main()
