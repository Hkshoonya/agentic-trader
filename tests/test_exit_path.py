"""The way out of a position: does a sell actually happen when it should?

These tests verify the runtime/strategy contract from the outside — they are
written against the behaviour the operator depends on, not against the
implementation that provides it:

1. a live fill reaches the strategy, so it can ask to sell what it holds;
2. a day's decision that was refused for a *technical* reason is not spent —
   the strategy gets to decide again instead of waiting for tomorrow;
3. a decision spent on a real risk refusal is not retried;
4. a position that has been sold is not sold twice.

The guard's own rules (a held symbol is always closable, a sell does not spend
the entry budget, an oversell is still refused) live in ``test_risk.py``.
"""

from __future__ import annotations

import json
import tempfile
import time
import unittest
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from unittest import mock

from agentic_trading.broker import Broker
from agentic_trading.config import load_config
from agentic_trading.history import Bar, save_bars
from agentic_trading.history_sync import bar_stem
from agentic_trading.runtime import RETRY, _Loop
from agentic_trading.strategies.trend_crypto import TrendCryptoStrategy
from agentic_trading.types import OrderIntent, Side
from tests.fakes import FakeMcpClient

FIXTURES = Path(__file__).parent / "fixtures"


def _bars(symbol: str, closes: list[float]) -> list[Bar]:
    start = datetime(2019, 1, 1, tzinfo=timezone.utc)
    return [
        Bar(
            symbol=bar_stem(symbol),
            start=start + timedelta(days=index),
            open=Decimal(str(close)),
            high=Decimal(str(close * 1.01)),
            low=Decimal(str(close * 0.99)),
            close=Decimal(str(close)),
            volume=Decimal("10000"),
        )
        for index, close in enumerate(closes)
    ]


def _rising(count: int = 300) -> list[float]:
    return [100.0 * (1.002**index) for index in range(count)]


def _falling(count: int = 300) -> list[float]:
    return [100.0 * (0.998**index) for index in range(count)]


def _quote(symbol: str, *, bid: str, ask: str, at: str) -> dict:
    return {
        "symbol": symbol,
        "observed_at": at,
        "quote_at": at,
        "bid": Decimal(bid),
        "ask": Decimal(ask),
    }


def _trend_strategy(tmp: Path) -> TrendCryptoStrategy:
    """BTC is the trend still worth holding; ETH's trend has broken."""
    bars = tmp / "bars"
    bars.mkdir(parents=True, exist_ok=True)
    save_bars(bars / f"{bar_stem('BTC-USD')}_day.jsonl", _bars("BTC-USD", _rising()))
    save_bars(bars / f"{bar_stem('ETH-USD')}_day.jsonl", _bars("ETH-USD", _falling()))
    strategy = TrendCryptoStrategy(
        bar_dir=bars,
        symbols=["BTC-USD", "ETH-USD"],
        state_path=tmp / "state" / "strategy_trend_crypto.json",
    )
    strategy.seed_positions({"ETH-USD": "0.5"})
    return strategy


def _config(tmp: Path, *, mode: str = "live") -> Path:
    config_path = tmp / "agentic.toml"
    config_path.write_text(
        "\n".join(
            [
                f'mode = "{mode}"',
                'symbol_whitelist = ["SPY", "BTC-USD", "ETH-USD"]',
                'max_order_pct = "0.50"',
                'daily_notional_pct = "0.90"',
                'daily_loss_pct = "0.03"',
                "max_open_positions = 4",
                "equity_refresh_ticks = 30",
                "equity_refresh_seconds = 60",
                'timezone = "local"',
                f'quotes_path = "{tmp / "quotes.jsonl"}"',
                f'journal_dir = "{tmp / "journal"}"',
                f'state_dir = "{tmp / "state"}"',
                f'tools_snapshot_path = "{tmp / "tools.json"}"',
                f'token_path = "{tmp / "missing.json"}"',
                'mcp_url = "https://agent.robinhood.com/mcp/trading"',
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    return config_path


def _tools() -> list[dict]:
    return json.loads((FIXTURES / "tools_snapshot.json").read_text())["tools"]


class _FakePositionStrategy:
    """A stand-in for the trend strategy: it tracks what it believes it holds."""

    def __init__(self) -> None:
        self.positions: dict[str, Decimal] = {}

    def on_quote(self, quote: dict) -> list[OrderIntent]:
        return []

    def seed_positions(self, positions: dict) -> int:
        self.positions = {
            str(symbol): Decimal(str(quantity))
            for symbol, quantity in positions.items()
            if Decimal(str(quantity)) > 0
        }
        return len(self.positions)


class StrategyExitRetryTests(unittest.TestCase):
    """The strategy's side: an unfilled exit is asked for again and again."""

    def test_an_unfilled_exit_is_re_emitted_at_the_freshest_price(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_name:
            tmp = Path(tmp_name)
            strategy = _trend_strategy(tmp)
            first = strategy.on_quote(
                _quote(
                    "BTC-USD",
                    bid="65000",
                    ask="65010",
                    at="2026-09-17T00:00:05Z",
                )
            )
            exits = [intent for intent in first if intent.side is Side.SELL]
            self.assertEqual([intent.symbol for intent in exits], ["ETH-USD"])
            # Crypto is its own book: the day marker is per book, so an equity
            # rebalance later the same day cannot spend the crypto decision.
            self.assertEqual(strategy.last_decided_day, "crypto:2026-09-17")
            # No ETH quote has been seen, so the exit falls back to the daily
            # close — the best price the strategy actually has.
            self.assertEqual(exits[0].ref_price, Decimal(str(_falling()[-1])))

            # The sell did not fill. A later ETH quote re-emits the exit at the
            # price that is executable now: an exit removes risk, so it is not
            # allowed to be a once-a-day decision that quietly failed.
            again = strategy.on_quote(
                _quote(
                    "ETH-USD",
                    bid="90.00",
                    ask="90.10",
                    at="2026-09-17T00:30:00Z",
                )
            )
            exits = [intent for intent in again if intent.side is Side.SELL]
            self.assertEqual([intent.symbol for intent in exits], ["ETH-USD"])
            self.assertEqual(exits[0].ref_price, Decimal("90.00"))
            self.assertNotEqual(exits[0].decision_id, first[0].decision_id)

    def test_a_filled_exit_is_not_re_emitted(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_name:
            tmp = Path(tmp_name)
            strategy = _trend_strategy(tmp)
            strategy.on_quote(
                _quote(
                    "BTC-USD",
                    bid="65000",
                    ask="65010",
                    at="2026-09-17T00:00:05Z",
                )
            )
            strategy.note_fill("ETH-USD", Decimal("-0.5"))
            after = strategy.on_quote(
                _quote(
                    "ETH-USD",
                    bid="90.00",
                    ask="90.10",
                    at="2026-09-17T00:31:00Z",
                )
            )
            self.assertEqual([i for i in after if i.side is Side.SELL], [])

    def test_an_entry_is_still_a_once_a_day_decision(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_name:
            tmp = Path(tmp_name)
            strategy = _trend_strategy(tmp)
            first = strategy.on_quote(
                _quote(
                    "BTC-USD",
                    bid="65000",
                    ask="65010",
                    at="2026-09-17T00:00:05Z",
                )
            )
            self.assertTrue([i for i in first if i.side is Side.BUY])
            second = strategy.on_quote(
                _quote(
                    "BTC-USD",
                    bid="65000",
                    ask="65010",
                    at="2026-09-17T01:00:00Z",
                )
            )
            self.assertEqual([i for i in second if i.side is Side.BUY], [])

    def test_a_stale_day_cannot_be_released(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_name:
            tmp = Path(tmp_name)
            strategy = _trend_strategy(tmp)
            strategy.on_quote(
                _quote(
                    "BTC-USD",
                    bid="65000",
                    ask="65010",
                    at="2026-09-17T00:00:05Z",
                )
            )
            self.assertFalse(strategy.release_decision("crypto:2026-09-16"))

    def test_releasing_the_day_clears_it(self) -> None:
        """The runtime's retry path needs the day given back, not just ignored."""
        with tempfile.TemporaryDirectory() as tmp_name:
            tmp = Path(tmp_name)
            strategy = _trend_strategy(tmp)
            strategy.on_quote(
                _quote(
                    "BTC-USD",
                    bid="65000",
                    ask="65010",
                    at="2026-09-17T00:00:05Z",
                )
            )
            self.assertTrue(strategy.release_decision("crypto:2026-09-17"))
            self.assertEqual(strategy.last_decided_day, "")
            again = strategy.on_quote(
                _quote(
                    "BTC-USD",
                    bid="65000",
                    ask="65010",
                    at="2026-09-17T00:31:00Z",
                )
            )
            self.assertTrue([i for i in again if i.side is Side.BUY])


class RuntimeReconcileTests(unittest.TestCase):
    """The runtime's side: the broker's book reaches the strategy."""

    def _loop(self, tmp: Path, client: FakeMcpClient, strategy=None) -> _Loop:
        client.tools = _tools()
        broker = Broker(client, client.tools)
        config = load_config(_config(tmp))
        return _Loop(config, broker, strategy)

    def test_a_live_hold_is_handed_to_the_strategy_once(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_name:
            tmp = Path(tmp_name)
            client = FakeMcpClient(
                [], positions=[{"symbol": "SPY", "quantity": "1.5"}]
            )
            strategy = _FakePositionStrategy()
            loop = self._loop(tmp, client, strategy)
            loop.mode = "live"
            self.assertTrue(loop.reconcile_strategy_positions())
            self.assertEqual(strategy.positions, {"SPY": Decimal("1.5")})
            # An unchanged book is not re-told: the strategy must not be
            # re-seeded (and the broker not re-read) on every cycle.
            self.assertFalse(loop.reconcile_strategy_positions())
            events = [record.get("event") for record in loop.journal.iter_today()]
            self.assertIn("strategy_reconciled", events)

    def test_shadow_does_not_read_the_broker_book_here(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_name:
            tmp = Path(tmp_name)
            client = FakeMcpClient(
                [], positions=[{"symbol": "SPY", "quantity": "1.5"}]
            )
            strategy = _FakePositionStrategy()
            loop = self._loop(tmp, client, strategy)
            loop.mode = "shadow"
            self.assertFalse(loop.reconcile_strategy_positions())
            self.assertEqual(client.calls_named("get_equity_positions"), [])

    def test_a_failed_position_read_does_not_wipe_the_strategy_book(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_name:
            tmp = Path(tmp_name)
            client = FakeMcpClient([])
            strategy = _FakePositionStrategy()
            strategy.positions = {"SPY": Decimal("1")}
            loop = self._loop(tmp, client, strategy)
            loop.mode = "live"
            with mock.patch.object(
                loop.broker, "get_positions", side_effect=RuntimeError("network")
            ):
                self.assertFalse(loop.reconcile_strategy_positions())
            self.assertEqual(strategy.positions, {"SPY": Decimal("1")})

    def test_a_placement_immediately_pushes_the_book(self) -> None:
        from agentic_trading.runtime import build_order_request

        with tempfile.TemporaryDirectory() as tmp_name:
            tmp = Path(tmp_name)
            client = FakeMcpClient(
                [], positions=[{"symbol": "SPY", "quantity": "2"}]
            )
            strategy = _FakePositionStrategy()
            loop = self._loop(tmp, client, strategy)
            loop.mode = "live"
            intent = OrderIntent(
                decision_id="buy-1",
                symbol="SPY",
                side=Side.BUY,
                quantity=Decimal("1"),
                ref_price=Decimal("100"),
                reason="test",
                created_at=datetime(2026, 9, 17, tzinfo=timezone.utc),
            )
            request = build_order_request(
                intent,
                account_number="FAKE0001",
                config=loop.config,
                session="regular",
            )
            loop._place(intent, request)
            self.assertEqual(strategy.positions, {"SPY": Decimal("2")})


class RebalanceRetryTests(unittest.TestCase):
    """A technically-refused day is returned, a risk-refused day is not."""

    def _loop(self, tmp: Path) -> _Loop:
        client = FakeMcpClient(_tools())
        broker = Broker(client, client.tools)
        return _Loop(load_config(_config(tmp)), broker, _FakePositionStrategy())

    def _deciding_strategy(self, loop: _Loop) -> None:
        loop.strategy.last_decided_day = "2026-09-17"
        loop.strategy.release_decision = lambda day: day == "2026-09-17"  # type: ignore[attr-defined]

    def test_all_technical_refusals_release_the_day(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_name:
            tmp = Path(tmp_name)
            loop = self._loop(tmp)
            self._deciding_strategy(loop)
            self.assertTrue(loop.maybe_retry_rebalance([RETRY, RETRY]))
            self.assertGreater(loop._rebalance_retry_at, time.monotonic())
            events = [record.get("event") for record in loop.journal.iter_today()]
            self.assertIn("rebalance_retry", events)

    def test_a_risk_refusal_does_not_release_the_day(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_name:
            tmp = Path(tmp_name)
            loop = self._loop(tmp)
            self._deciding_strategy(loop)
            self.assertFalse(loop.maybe_retry_rebalance([None]))
            self.assertFalse(loop.maybe_retry_rebalance([RETRY, None]))
            events = [record.get("event") for record in loop.journal.iter_today()]
            self.assertNotIn("rebalance_retry", events)

    def test_retries_stop_at_the_configured_limit(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_name:
            tmp = Path(tmp_name)
            loop = self._loop(tmp)
            loop.config = loop.config.__class__(
                **{**loop.config.__dict__, "rebalance_max_retries": 1}
            )
            self._deciding_strategy(loop)
            self.assertTrue(loop.maybe_retry_rebalance([RETRY]))
            self.assertFalse(loop.maybe_retry_rebalance([RETRY]))
            events = [record.get("event") for record in loop.journal.iter_today()]
            self.assertIn("rebalance_abandoned", events)


class WorkingExitTests(unittest.TestCase):
    """An exit already working is not sent twice; it is also not forgotten."""

    def _loop(self, tmp: Path, client: FakeMcpClient, strategy=None) -> _Loop:
        client.tools = _tools()
        broker = Broker(client, client.tools)
        return _Loop(load_config(_config(tmp)), broker, strategy)

    def test_a_sell_in_flight_suppresses_the_repeat_but_not_silently(self) -> None:
        from agentic_trading.types import new_decision_id

        with tempfile.TemporaryDirectory() as tmp_name:
            tmp = Path(tmp_name)
            client = FakeMcpClient([])
            loop = self._loop(tmp, client)
            loop.open_order_symbols = {"ETH-USD"}
            loop.open_order_sides = {"ETH-USD": {"sell"}}
            for _ in range(3):
                loop.process_intent(
                    OrderIntent(
                        decision_id=new_decision_id(),
                        symbol="ETH-USD",
                        side=Side.SELL,
                        quantity=Decimal("0.5"),
                        ref_price=Decimal("90"),
                        reason="trend_exit",
                        created_at=datetime(2026, 9, 17, tzinfo=timezone.utc),
                    ),
                    session="regular",
                )
            records = list(loop.journal.iter_today())
            skipped = [r for r in records if r.get("event") == "exit_skipped"]
            # One journal line per working exit, not one per quote: the point
            # is to explain the silence, not to flood the journal with it.
            self.assertEqual(len(skipped), 1)
            self.assertEqual(client.calls_named("preview_crypto_order"), [])
            self.assertEqual(client.calls_named("place_crypto_order"), [])


if __name__ == "__main__":
    unittest.main()
