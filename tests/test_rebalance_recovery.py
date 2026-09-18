"""A once-a-day decision must not be spent on a technical failure.

On 2026-09-18 the live daemon took its one daily rebalance at 00:00 UTC, while
the per-order cap was still below the broker's $1 minimum. All five entries were
refused as ``below_min_notional``, the strategy had already recorded the day as
decided, and the book traded nothing for twenty-four hours. The refusal was
correct; spending the day on it was not.

These tests pin the recovery: a decision is handed back when *every* order in it
failed for a technical reason, retried on a leash, and never handed back when a
risk rule was the thing that said no.
"""

from __future__ import annotations

import tempfile
import unittest
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any

from agentic_trading.config import load_config
from agentic_trading.risk import PortfolioSnapshot
from agentic_trading.runtime import RETRY, _Loop
from agentic_trading.types import OrderIntent, Side


def _config(tmp: Path, **overrides: Any) -> Any:
    values: dict[str, str] = {
        "mode": '"live"',
        "strategy": '"fixture"',
        "symbol_whitelist": '["SPY"]',
        "max_order_pct": '"0.05"',
        "daily_notional_pct": '"0.20"',
        "daily_loss_pct": '"0.03"',
        "max_open_positions": "4",
        "equity_refresh_ticks": "30",
        "equity_refresh_seconds": "60",
        "timezone": '"local"',
        "quotes_path": f'"{tmp / "quotes.jsonl"}"',
        "journal_dir": f'"{tmp / "journal"}"',
        "state_dir": f'"{tmp / "state"}"',
        "tools_snapshot_path": f'"{tmp / "tools.json"}"',
        "token_path": f'"{tmp / "missing.json"}"',
        "mcp_url": '"https://agent.robinhood.com/mcp/trading"',
    }
    values.update({key: str(value) for key, value in overrides.items()})
    lines = [f"{key} = {value}" for key, value in values.items()]
    path = tmp / "agentic.toml"
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return load_config(path)


class _DailyStrategy:
    """A stand-in for the trend strategy: one entry a day, releasable."""

    def __init__(self) -> None:
        self.last_decided_day = ""
        self.released: list[str] = []
        self.buys = 0

    def on_quote(self, quote: dict) -> list[OrderIntent]:
        if self.last_decided_day:
            return []
        self.buys += 1
        self.last_decided_day = "crypto:2026-09-18"
        return [
            OrderIntent(
                decision_id=f"buy-{self.buys}",
                symbol="SPY",
                side=Side.BUY,
                quantity=Decimal("1"),
                ref_price=Decimal("100"),
                reason="trend_entry",
                created_at=datetime(2026, 9, 18, tzinfo=timezone.utc),
            )
        ]

    def release_decision(self, day: str) -> bool:
        if day != self.last_decided_day:
            return False
        self.released.append(day)
        self.last_decided_day = ""
        return True


class _RecordingStrategy:
    def __init__(self) -> None:
        self.seeded: list[dict[str, str]] = []

    def on_quote(self, quote: dict) -> list[OrderIntent]:
        return []

    def seed_positions(self, positions: dict[str, Any]) -> int:
        self.seeded.append({str(k): str(v) for k, v in positions.items()})
        return len(positions)


class _StubBroker:
    """Only the read surface the reconcile path uses."""

    def __init__(self, held: dict[str, Decimal] | None = None) -> None:
        self.held = held or {}
        self.reads = 0
        self.fail = False

    def get_positions(self) -> PortfolioSnapshot:
        self.reads += 1
        if self.fail:
            raise RuntimeError("gateway down")
        return PortfolioSnapshot(open_positions=len(self.held), held=dict(self.held))


def _quote() -> dict:
    return {
        "symbol": "SPY",
        "observed_at": "2026-09-18T00:00:05Z",
        "quote_at": "2026-09-18T00:00:00Z",
        "bid": Decimal("99.99"),
        "ask": Decimal("100.01"),
    }


def _events(loop: _Loop) -> list[dict]:
    return list(loop.journal.iter_today())


def _event_names(loop: _Loop) -> list[str]:
    return [str(record.get("event")) for record in _events(loop)]


class RebalanceRetryTests(unittest.TestCase):
    def test_an_unknown_account_value_defers_instead_of_spending_the_day(self) -> None:
        """Equity is read a moment after start-up; the decision must wait for it."""
        with tempfile.TemporaryDirectory() as name:
            strategy = _DailyStrategy()
            loop = _Loop(_config(Path(name)), _StubBroker(), strategy)
            loop.guard.current_equity = Decimal("0")  # nothing read yet

            loop.handle_quote(_quote(), session="regular")

            events = _events(loop)
            self.assertIn("decision_deferred", [e.get("event") for e in events])
            deferred = next(e for e in events if e["event"] == "decision_deferred")
            self.assertEqual(deferred["reason"], "equity_pending")
            self.assertEqual(strategy.released, ["crypto:2026-09-18"])
            retry = next(e for e in events if e["event"] == "rebalance_retry")
            self.assertEqual(retry["attempt"], 1)
            self.assertEqual(retry["of"], 6)

    def test_a_risk_refusal_is_final_for_the_day(self) -> None:
        """A cap that fired is a decision, not a hiccup: retrying is chasing."""
        with tempfile.TemporaryDirectory() as name:
            strategy = _DailyStrategy()
            loop = _Loop(
                _config(Path(name), max_order_pct='"0.0001"'),
                _StubBroker(),
                strategy,
            )
            loop.guard.current_equity = Decimal("50")  # half a cent per order

            loop.handle_quote(_quote(), session="regular")

            names = _event_names(loop)
            self.assertEqual(strategy.released, [])
            self.assertNotIn("rebalance_retry", names)
            self.assertIn("rejected", names)

    def test_the_retry_is_on_a_leash_and_gives_up(self) -> None:
        with tempfile.TemporaryDirectory() as name:
            strategy = _DailyStrategy()
            loop = _Loop(
                _config(
                    Path(name),
                    rebalance_retry_seconds="900",
                    rebalance_max_retries="2",
                ),
                _StubBroker(),
                strategy,
            )
            loop.guard.current_equity = Decimal("0")

            loop.handle_quote(_quote(), session="regular")  # attempt 1
            # A quote inside the window must not re-run the rebalance at all.
            loop.handle_quote(_quote(), session="regular")
            self.assertEqual(strategy.buys, 1)

            loop._rebalance_retry_at = 0.0  # the window closed
            loop.handle_quote(_quote(), session="regular")  # attempt 2
            loop._rebalance_retry_at = 0.0
            loop.handle_quote(_quote(), session="regular")  # past the limit

            names = _event_names(loop)
            self.assertEqual(names.count("rebalance_retry"), 2)
            self.assertIn("rebalance_abandoned", names)


class StrategyReconcileTests(unittest.TestCase):
    """The strategy's book must follow the account, not the start-up snapshot."""

    def _loop(self, tmp: Path, broker: _StubBroker) -> _Loop:
        loop = _Loop(_config(tmp), broker, _RecordingStrategy())
        loop.mode = "live"
        return loop

    def test_the_book_is_handed_over_and_only_speaks_when_it_changes(self) -> None:
        with tempfile.TemporaryDirectory() as name:
            broker = _StubBroker({"BTC-USD": Decimal("0.5")})
            loop = self._loop(Path(name), broker)

            self.assertTrue(loop.reconcile_strategy_positions())
            self.assertFalse(loop.reconcile_strategy_positions())  # unchanged
            self.assertEqual(broker.reads, 2)

            broker.held = {"BTC-USD": Decimal("0.25")}
            self.assertTrue(loop.reconcile_strategy_positions())

            changes = [
                e for e in _events(loop) if e.get("event") == "strategy_reconciled"
            ]
            self.assertEqual(len(changes), 2)
            self.assertEqual(changes[0]["held"], ["BTC-USD"])
            self.assertEqual(loop._strategy_book, {"BTC-USD": "0.25"})

    def test_a_failed_read_never_wipes_the_book(self) -> None:
        with tempfile.TemporaryDirectory() as name:
            broker = _StubBroker({"SPY": Decimal("2")})
            loop = self._loop(Path(name), broker)
            self.assertTrue(loop.reconcile_strategy_positions())

            broker.fail = True
            self.assertFalse(loop.reconcile_strategy_positions())
            self.assertEqual(loop._strategy_book, {"SPY": "2"})
            self.assertIn("strategy_reconcile_failed", _event_names(loop))

    def test_a_shadow_loop_does_not_reconcile_from_the_broker(self) -> None:
        """In shadow the runtime applies its own fills; the broker is not truth."""
        with tempfile.TemporaryDirectory() as name:
            broker = _StubBroker({"SPY": Decimal("1")})
            loop = _Loop(_config(Path(name)), broker, _RecordingStrategy())
            loop.mode = "shadow"
            self.assertFalse(loop.reconcile_strategy_positions())
            self.assertEqual(broker.reads, 0)


class UniverseAdoptionTests(unittest.TestCase):
    """The scout's decision has to reach the running loop without a restart."""

    def test_adopting_adds_to_the_guard_and_rebuilds_the_strategy(self) -> None:
        with tempfile.TemporaryDirectory() as name:
            built: list[frozenset[str]] = []

            def factory(config: Any) -> _RecordingStrategy:
                built.append(config.effective_whitelist)
                return _RecordingStrategy()

            loop = _Loop(
                _config(Path(name)),
                _StubBroker(),
                _RecordingStrategy(),
                strategy_factory=factory,
            )
            self.assertTrue(loop.adopt_universe(["AMD"]))

            self.assertIn("AMD", loop.config.effective_whitelist)
            self.assertIn("AMD", loop.guard.whitelist)
            self.assertEqual(loop.config.symbol_whitelist, frozenset({"SPY"}))
            self.assertEqual(loop.config.discovered_symbols, frozenset({"AMD"}))
            self.assertTrue(loop.universe_dirty)
            self.assertEqual(built, [frozenset({"SPY", "AMD"})])
            self.assertIn("universe_changed", _event_names(loop))

    def test_an_unchanged_universe_is_not_a_change(self) -> None:
        with tempfile.TemporaryDirectory() as name:
            loop = _Loop(_config(Path(name)), _StubBroker(), _RecordingStrategy())
            self.assertFalse(loop.adopt_universe(["SPY"]))  # already core
            self.assertFalse(loop.universe_dirty)

    def test_a_strategy_that_cannot_be_rebuilt_keeps_the_working_universe(self) -> None:
        with tempfile.TemporaryDirectory() as name:
            def factory(config: Any) -> _RecordingStrategy:
                raise RuntimeError("cannot build for this universe")

            loop = _Loop(
                _config(Path(name)),
                _StubBroker(),
                _RecordingStrategy(),
                strategy_factory=factory,
            )
            self.assertFalse(loop.adopt_universe(["AMD"]))
            self.assertEqual(loop.config.discovered_symbols, frozenset())
            self.assertEqual(loop.guard.whitelist, frozenset({"SPY"}))
            self.assertFalse(loop.universe_dirty)


class RetryContractTests(unittest.TestCase):
    def test_a_weighted_order_that_cannot_clear_the_minimum_is_placed_at_it(self) -> None:
        """The live 00:00 UTC refusal, reproduced.

        $50 of equity, the per-order cap raised to 2.04% so a $1.02 order is
        possible, and a LINK-sized weight of 0.33 on the intent. Scaling first
        and refusing second threw the trade away; the minimum is what the
        operator's small-account mode authorised, so it is what gets placed.
        """
        from agentic_trading.sizer import size_intent

        intent = OrderIntent(
            decision_id="weighted",
            symbol="LINK-USD",
            side=Side.BUY,
            quantity=Decimal("1"),
            ref_price=Decimal("11.793"),
            reason="trend_entry",
            created_at=datetime(2026, 9, 18, tzinfo=timezone.utc),
            weight=Decimal("0.331"),
        )
        sized = size_intent(
            intent,
            equity=Decimal("50"),
            max_order_pct=Decimal("0.0204"),
            min_notional=Decimal("1.00"),
            proportional=True,
        )
        self.assertIsNotNone(sized, "a weighted order was refused at the minimum")
        assert sized is not None
        notional = sized.quantity * sized.ref_price
        self.assertGreaterEqual(notional, Decimal("1.00"))
        self.assertLessEqual(notional, Decimal("1.02"))

    def test_the_floor_never_sizes_up_past_the_unweighted_ceiling(self) -> None:
        """The floor places the minimum; it is not a licence to trade bigger."""
        from agentic_trading.sizer import size_intent

        intent = OrderIntent(
            decision_id="weighted-small-cap",
            symbol="LINK-USD",
            side=Side.BUY,
            quantity=Decimal("1"),
            ref_price=Decimal("11.793"),
            reason="trend_entry",
            created_at=datetime(2026, 9, 18, tzinfo=timezone.utc),
            weight=Decimal("0.10"),
        )
        # A ceiling of 0.9% of $50 is $0.45 — under the minimum, so the trade is
        # still refused rather than inflated to the floor.
        self.assertIsNone(
            size_intent(
                intent,
                equity=Decimal("50"),
                max_order_pct=Decimal("0.009"),
                min_notional=Decimal("1.00"),
                proportional=True,
            )
        )
        # And with a ceiling that does clear it, the order lands on the minimum,
        # not on a weight-free $5.
        sized = size_intent(
            intent,
            equity=Decimal("500"),
            max_order_pct=Decimal("0.01"),
            min_notional=Decimal("1.00"),
            proportional=True,
        )
        self.assertIsNotNone(sized)
        assert sized is not None
        self.assertLessEqual(sized.quantity * sized.ref_price, Decimal("1.02"))

    def test_the_retry_sentinel_is_a_named_constant(self) -> None:
        """The value is part of the contract between the loop and its callers."""
        self.assertEqual(RETRY, "retry")
        self.assertNotEqual(RETRY, None)


if __name__ == "__main__":
    unittest.main()
