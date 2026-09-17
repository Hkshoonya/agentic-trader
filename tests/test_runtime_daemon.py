"""Autonomous daemon loop: session gating, live gating, staleness, failures."""

from __future__ import annotations

import json
import os
import tempfile
import unittest
from datetime import date, datetime, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any
from unittest import mock

from agentic_trading.broker import Broker
from agentic_trading.config import load_config
from agentic_trading.runtime import run_daemon
from agentic_trading.strategies.fixture import FixtureStrategy
from agentic_trading.types import OrderIntent, Side
from tests.fakes import FakeMcpClient
from tests.schema import tool_schema, validate

FIXTURES = Path(__file__).parent / "fixtures"
TOOLS_SNAPSHOT = FIXTURES / "tools_snapshot.json"

# Quotes are stamped 2026-09-16T14:00:SSZ; pin the daemon clock just after them
# so freshness checks are deterministic.
FIXED_NOW = datetime(2026, 9, 16, 14, 0, 30, tzinfo=timezone.utc)


def load_tools() -> list[dict]:
    return json.loads(TOOLS_SNAPSHOT.read_text(encoding="utf-8"))["tools"]


def _quote(bid: str = "100.00", ask: str = "100.02", second: int = 0) -> dict:
    return {
        "symbol": "SPY",
        "observed_at": f"2026-09-16T14:00:{second:02d}Z",
        "quote_at": f"2026-09-16T13:59:{second:02d}Z",
        "bid": Decimal(bid),
        "ask": Decimal(ask),
    }


class _StubFeed:
    def __init__(self, quotes: list[dict]) -> None:
        self.quotes = list(quotes)
        self.polls = 0

    def poll(self) -> list[dict]:
        self.polls += 1
        pending, self.quotes = self.quotes, []
        return pending


def _failing_review(original):
    """Wrap a fake client so review calls raise, everything else works."""

    def call(name: str, arguments: dict) -> dict:
        if name == "review_equity_order":
            raise RuntimeError("review unavailable")
        return original(name, arguments)

    return call


class _AlwaysBuy:
    def on_quote(self, quote: dict) -> list[OrderIntent]:
        return [
            OrderIntent(
                decision_id=f"buy-{quote['observed_at']}",
                symbol=quote["symbol"],
                side=Side.BUY,
                quantity=Decimal("0.01"),
                ref_price=quote["ask"],
                reason="test:buy",
                created_at=datetime(2026, 9, 16, 14, 0, tzinfo=timezone.utc),
            )
        ]


def _write_config(
    tmp: Path, *, mode: str = "shadow", extra: list[str] | None = None
) -> Path:
    config_path = tmp / "agentic.toml"
    lines = [
        f'mode = "{mode}"',
        'symbol_whitelist = ["SPY"]',
        'max_order_pct = "0.05"',
        'daily_notional_pct = "0.20"',
        'daily_loss_pct = "0.03"',
        "max_open_positions = 1",
        "equity_refresh_ticks = 30",
        "equity_refresh_seconds = 60",
        'timezone = "local"',
        f'quotes_path = "{tmp / "quotes.jsonl"}"',
        f'journal_dir = "{tmp / "journal"}"',
        f'state_dir = "{tmp / "state"}"',
        f'tools_snapshot_path = "{tmp / "tools_snapshot.json"}"',
        f'token_path = "{tmp / "missing-tokens.json"}"',
        'mcp_url = "https://agent.robinhood.com/mcp/trading"',
    ]
    lines.extend(extra or [])
    config_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return config_path


def _records(config) -> list[dict]:
    path = Path(config.journal_dir) / f"{date.today().isoformat()}.jsonl"
    if not path.is_file():
        return []
    return [
        json.loads(line)
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]


def _decision_events(config) -> list[str]:
    """Journal events minus the startup arming record."""
    return [
        record["event"]
        for record in _records(config)
        if record.get("event") != "live_gate"
    ]


def _run(config, broker, strategy, feed, *, session: str = "regular", **kwargs: Any):
    kwargs.setdefault("once", True)
    kwargs.setdefault("clock", lambda: FIXED_NOW)
    kwargs.setdefault("session_clock", lambda: session)
    return run_daemon(config, broker=broker, strategy=strategy, feed=feed, **kwargs)


class DaemonShadowTests(unittest.TestCase):
    def test_cycle_stats_heartbeat_reports_measured_cadence(self) -> None:
        """Without a heartbeat a quiet strategy is indistinguishable from a
        slow loop; the daemon journals its own measured cycle time."""
        tools = load_tools()
        client = FakeMcpClient(tools)
        broker = Broker(client, tools)

        class _RepeatingFeed:
            def poll(self) -> list[dict]:
                return [_quote()]

        with tempfile.TemporaryDirectory() as tmp_name:
            tmp = Path(tmp_name)
            config = load_config(
                _write_config(
                    tmp,
                    extra=["poll_seconds = 0.01", "cycle_stats_seconds = 0.01"],
                )
            )
            _run(
                config,
                broker,
                FixtureStrategy(),
                _RepeatingFeed(),
                once=False,
                duration_seconds=0.4,
                sleep=lambda _seconds: None,
            )

            stats = [r for r in _records(config) if r.get("event") == "cycle_stats"]
            self.assertTrue(stats, "no cycle_stats heartbeat was written")
            record = stats[-1]
            self.assertGreaterEqual(record["cycles"], 1)
            self.assertIsInstance(record["avg_cycle_seconds"], float)
            self.assertGreater(record["fresh_quotes"], 0)
            self.assertIn("fresh_quotes", record)

    def test_single_cycle_journals_and_never_places(self) -> None:
        tools = load_tools()
        client = FakeMcpClient(tools)
        broker = Broker(client, tools)

        with tempfile.TemporaryDirectory() as tmp_name:
            tmp = Path(tmp_name)
            config = load_config(_write_config(tmp))
            _run(config, broker, FixtureStrategy(), _StubFeed([_quote()]))

            records = _records(config)
            accepted = [r for r in records if r.get("event") == "accepted"]
            self.assertEqual(len(accepted), 1)
            self.assertEqual(client.calls_named("place_equity_order"), [])
            self.assertEqual(accepted[0]["session"], "regular")
            self.assertEqual(
                accepted[0]["order_request"]["dollar_amount"], "1.00"
            )
            self.assertTrue(accepted[0]["order_request"]["account_number"])

    def test_closed_session_does_no_work(self) -> None:
        tools = load_tools()
        client = FakeMcpClient(tools)
        broker = Broker(client, tools)

        with tempfile.TemporaryDirectory() as tmp_name:
            tmp = Path(tmp_name)
            config = load_config(_write_config(tmp))
            _run(
                config,
                broker,
                FixtureStrategy(),
                _StubFeed([_quote()]),
                session="weekend",
            )
            records = _records(config)
            self.assertEqual(_decision_events(config), ["session_closed"])
            self.assertEqual(client.calls_named("review_equity_order"), [])
            self.assertEqual(client.calls_named("place_equity_order"), [])

    def test_stale_quotes_are_rejected_before_any_strategy(self) -> None:
        tools = load_tools()
        client = FakeMcpClient(tools)
        broker = Broker(client, tools)
        stale = {
            "symbol": "SPY",
            "observed_at": "2026-09-15T14:00:00Z",  # a day old
            "quote_at": "2026-09-15T13:59:59Z",
            "bid": Decimal("100.00"),
            "ask": Decimal("100.02"),
        }

        with tempfile.TemporaryDirectory() as tmp_name:
            tmp = Path(tmp_name)
            config = load_config(_write_config(tmp))
            _run(config, broker, FixtureStrategy(), _StubFeed([stale]))

            records = _records(config)
            self.assertEqual(
                _decision_events(config), ["stale_quotes_rejected"]
            )
            self.assertEqual(client.calls_named("review_equity_order"), [])
            self.assertEqual(client.calls_named("place_equity_order"), [])

    def test_quote_without_timestamp_is_rejected(self) -> None:
        tools = load_tools()
        client = FakeMcpClient(tools)
        broker = Broker(client, tools)
        quote = _quote()
        quote.pop("observed_at")

        with tempfile.TemporaryDirectory() as tmp_name:
            tmp = Path(tmp_name)
            config = load_config(_write_config(tmp))
            _run(config, broker, FixtureStrategy(), _StubFeed([quote]))
            self.assertEqual(
                _decision_events(config), ["stale_quotes_rejected"]
            )


class DaemonLiveTests(unittest.TestCase):
    def test_live_places_schema_valid_order_when_gated(self) -> None:
        tools = load_tools()
        client = FakeMcpClient(tools)
        broker = Broker(client, tools)

        with tempfile.TemporaryDirectory() as tmp_name:
            tmp = Path(tmp_name)
            config = load_config(_write_config(tmp, mode="live"))
            with mock.patch.dict(os.environ, {"AGENTIC_ALLOW_LIVE": "1"}):
                _run(config, broker, FixtureStrategy(), _StubFeed([_quote()]))

            calls = client.calls_named("place_equity_order")
            self.assertEqual(len(calls), 1)
            self.assertEqual(
                validate(tool_schema(tools, "place_equity_order"), calls[0].arguments),
                [],
            )
            self.assertEqual(calls[0].arguments["side"], "buy")
            self.assertEqual(calls[0].arguments["type"], "market")
            records = _records(config)
            self.assertTrue(any(r.get("event") == "placed" for r in records))

    def test_live_without_environment_gate_does_not_place(self) -> None:
        tools = load_tools()
        client = FakeMcpClient(tools)
        broker = Broker(client, tools)

        with tempfile.TemporaryDirectory() as tmp_name:
            tmp = Path(tmp_name)
            config = load_config(_write_config(tmp, mode="live"))
            env = {k: v for k, v in os.environ.items() if k != "AGENTIC_ALLOW_LIVE"}
            with mock.patch.dict(os.environ, env, clear=True):
                _run(config, broker, FixtureStrategy(), _StubFeed([_quote()]))
            self.assertEqual(client.calls_named("place_equity_order"), [])
            self.assertTrue(any(r.get("event") == "accepted" for r in _records(config)))

    def test_dry_run_forces_shadow_even_when_mode_file_says_live(self) -> None:
        tools = load_tools()
        client = FakeMcpClient(tools)
        broker = Broker(client, tools)

        with tempfile.TemporaryDirectory() as tmp_name:
            tmp = Path(tmp_name)
            config = load_config(_write_config(tmp, mode="shadow"))
            state_dir = Path(config.state_dir)
            state_dir.mkdir(parents=True, exist_ok=True)
            (state_dir / "mode").write_text("live\n", encoding="utf-8")

            with mock.patch.dict(os.environ, {"AGENTIC_ALLOW_LIVE": "1"}):
                _run(
                    config,
                    broker,
                    FixtureStrategy(),
                    _StubFeed([_quote()]),
                    force_shadow=True,
                )
            self.assertEqual(client.calls_named("place_equity_order"), [])

    def test_pending_open_order_blocks_new_entry(self) -> None:
        tools = load_tools()
        client = FakeMcpClient(
            tools,
            orders=[{"id": "o1", "symbol": "SPY", "side": "buy", "state": "queued"}],
        )
        broker = Broker(client, tools)

        with tempfile.TemporaryDirectory() as tmp_name:
            tmp = Path(tmp_name)
            config = load_config(_write_config(tmp, mode="live"))
            with mock.patch.dict(os.environ, {"AGENTIC_ALLOW_LIVE": "1"}):
                _run(config, broker, FixtureStrategy(), _StubFeed([_quote()]))
            rejected = [r for r in _records(config) if r.get("event") == "rejected"]
            self.assertTrue(any(r["reason"] == "open_order_pending" for r in rejected))
            self.assertEqual(client.calls_named("place_equity_order"), [])

    def test_consecutive_place_errors_trip_kill_switch(self) -> None:
        tools = load_tools()
        client = FakeMcpClient(tools, place_error=RuntimeError("broker down"))
        broker = Broker(client, tools)

        with tempfile.TemporaryDirectory() as tmp_name:
            tmp = Path(tmp_name)
            config = load_config(
                _write_config(tmp, mode="live", extra=["max_consecutive_errors = 2"])
            )
            with mock.patch.dict(os.environ, {"AGENTIC_ALLOW_LIVE": "1"}):
                _run(
                    config,
                    broker,
                    _AlwaysBuy(),
                    _StubFeed(
                        [_quote(second=0), _quote(second=5), _quote(second=10)]
                    ),
                )
            records = _records(config)
            failures = [r for r in records if r.get("event") == "place_failed"]
            self.assertEqual(len(failures), 2)
            rejected = [r for r in records if r.get("event") == "rejected"]
            self.assertTrue(any(r["reason"] == "kill_switch" for r in rejected))
            state = json.loads(
                (Path(config.state_dir) / "risk_guard.json").read_text(encoding="utf-8")
            )
            self.assertTrue(state["kill_switch"])
            self.assertEqual(state["kill_reason"], "consecutive_place_failed")

    def test_review_failure_blocks_placement_and_is_journaled(self) -> None:
        tools = load_tools()
        client = FakeMcpClient(tools)
        client.call_tool = _failing_review(client.call_tool)  # type: ignore[assignment]
        broker = Broker(client, tools)

        with tempfile.TemporaryDirectory() as tmp_name:
            tmp = Path(tmp_name)
            config = load_config(_write_config(tmp, mode="live"))
            with mock.patch.dict(os.environ, {"AGENTIC_ALLOW_LIVE": "1"}):
                _run(config, broker, FixtureStrategy(), _StubFeed([_quote()]))

            self.assertEqual(client.calls_named("place_equity_order"), [])
            events = [r.get("event") for r in _records(config)]
            self.assertIn("review_failed", events)
            self.assertIn("error_streak", events)

    def test_max_orders_per_day_blocks_additional_entries(self) -> None:
        tools = load_tools()
        client = FakeMcpClient(tools)
        broker = Broker(client, tools)

        with tempfile.TemporaryDirectory() as tmp_name:
            tmp = Path(tmp_name)
            config = load_config(
                _write_config(tmp, mode="live", extra=["max_orders_per_day = 1"])
            )
            with mock.patch.dict(os.environ, {"AGENTIC_ALLOW_LIVE": "1"}):
                _run(
                    config,
                    broker,
                    _AlwaysBuy(),
                    _StubFeed([_quote(second=0), _quote(second=5)]),
                )
            self.assertEqual(len(client.calls_named("place_equity_order")), 1)
            rejected = [r for r in _records(config) if r.get("event") == "rejected"]
            self.assertTrue(any(r["reason"] == "max_orders_per_day" for r in rejected))

    def test_extended_session_uses_marketable_limit_for_whole_shares(self) -> None:
        """Outside regular hours only limit orders execute, so quantity rules."""

        class _WholeShareBuy:
            def on_quote(self, quote: dict) -> list[OrderIntent]:
                return [
                    OrderIntent(
                        decision_id="whole-share-buy",
                        symbol="SPY",
                        side=Side.BUY,
                        quantity=Decimal("1"),
                        ref_price=quote["ask"],
                        reason="test:whole",
                        created_at=datetime(2026, 9, 16, 14, 0, tzinfo=timezone.utc),
                    )
                ]

        tools = load_tools()
        client = FakeMcpClient(tools, equity="10000")
        broker = Broker(client, tools)

        with tempfile.TemporaryDirectory() as tmp_name:
            tmp = Path(tmp_name)
            config = load_config(
                _write_config(
                    tmp,
                    mode="live",
                    extra=['session_policy = "extended"'],
                )
            )
            with mock.patch.dict(os.environ, {"AGENTIC_ALLOW_LIVE": "1"}):
                _run(
                    config,
                    broker,
                    _WholeShareBuy(),
                    _StubFeed([_quote()]),
                    session="premarket",
                )
            calls = client.calls_named("place_equity_order")
            self.assertEqual(len(calls), 1)
            args = calls[0].arguments
            self.assertEqual(args["type"], "limit")
            self.assertEqual(args["market_hours"], "extended_hours")
            self.assertEqual(args["quantity"], "1")
            self.assertEqual(args["limit_price"], "100.02")
            self.assertEqual(
                validate(tool_schema(tools, "place_equity_order"), args), []
            )

    def test_fractional_order_outside_regular_hours_is_refused(self) -> None:
        tools = load_tools()
        client = FakeMcpClient(tools)
        broker = Broker(client, tools)

        with tempfile.TemporaryDirectory() as tmp_name:
            tmp = Path(tmp_name)
            config = load_config(
                _write_config(tmp, mode="live", extra=['session_policy = "extended"'])
            )
            with mock.patch.dict(os.environ, {"AGENTIC_ALLOW_LIVE": "1"}):
                _run(
                    config,
                    broker,
                    FixtureStrategy(),
                    _StubFeed([_quote()]),
                    session="premarket",
                )
            rejected = [r for r in _records(config) if r.get("event") == "rejected"]
            self.assertTrue(
                any(
                    str(r["reason"]).startswith("order_invalid: fractional")
                    for r in rejected
                ),
                rejected,
            )
            self.assertEqual(client.calls_named("place_equity_order"), [])


if __name__ == "__main__":
    unittest.main()


class StageApplicationTests(unittest.TestCase):
    """A stage reached on one path must be applied on every path.

    The walk-forward regrade path used to record a promotion and stop, leaving
    the agent promoted in state (stage probability) and shadow in practice: no
    mode flip, no budget, no journal trail.
    """

    def _loop(self, tmp: Path, *, mode: str = "shadow"):
        from agentic_trading.runtime import _Loop

        # autonomy = "auto" is the operator switch for self-promotion; the
        # environment switch (AGENTIC_ALLOW_AUTONOMY) is patched per test.
        config_path = _write_config(tmp, mode=mode, extra=['autonomy = "auto"'])
        config = load_config(config_path)
        client = FakeMcpClient(load_tools())
        broker = Broker(client, load_tools())
        (tmp / "state").mkdir(parents=True, exist_ok=True)
        return _Loop(config, broker, FixtureStrategy()), config

    def test_applying_a_promotion_flips_the_mode_and_journals_it(self) -> None:
        from agentic_trading import runtime as module
        from agentic_trading.promotion import PromotionState

        with tempfile.TemporaryDirectory() as name:
            tmp = Path(name)
            loop, config = self._loop(tmp)
            journal = mock.Mock()
            state = PromotionState(stage="probation")
            with mock.patch.dict(os.environ, {"AGENTIC_ALLOW_AUTONOMY": "1"}):
                module._apply_promotion(config, loop, journal, state)
        events = [call.args[0]["event"] for call in journal.append.call_args_list]
        self.assertIn("stage_applied", events)
        self.assertIn("autonomy_applied", events)
        self.assertEqual(loop.mode, "live")

    def test_without_consent_the_promotion_is_recorded_not_applied(self) -> None:
        from agentic_trading import runtime as module
        from agentic_trading.promotion import PromotionState

        with tempfile.TemporaryDirectory() as name:
            tmp = Path(name)
            loop, config = self._loop(tmp)
            journal = mock.Mock()
            with mock.patch.dict(os.environ, {"AGENTIC_ALLOW_AUTONOMY": "0"}):
                module._apply_promotion(
                    config, loop, journal, PromotionState(stage="probation")
                )
        events = [call.args[0]["event"] for call in journal.append.call_args_list]
        self.assertEqual(events, ["promotion_requires_consent"])
        self.assertEqual(loop.mode, "shadow")

    def test_reconcile_raises_the_mode_to_the_stage(self) -> None:
        from agentic_trading.promotion import PromotionState, save_state

        with tempfile.TemporaryDirectory() as name:
            tmp = Path(name)
            loop, config = self._loop(tmp)
            save_state(config.state_dir, PromotionState(stage="probation"))
            with mock.patch.dict(os.environ, {"AGENTIC_ALLOW_AUTONOMY": "1"}):
                event = loop.reconcile_stage_mode()
            written = Path(config.state_dir, "mode").read_text().strip()
        assert event is not None
        self.assertEqual(event["event"], "stage_mode_reconciled")
        self.assertEqual(loop.mode, "live")
        # The file matters as much as the in-memory flip: a restart reads it.
        self.assertEqual(written, "live")

    def test_reconcile_never_lowers_an_operator_live_mode(self) -> None:
        """mode = "live" in the config is an operator decision, not drift."""
        with tempfile.TemporaryDirectory() as name:
            tmp = Path(name)
            loop, config = self._loop(tmp, mode="live")
            self.assertIsNone(loop.reconcile_stage_mode())
            self.assertEqual(loop.mode, "live")

    def test_reconcile_is_blocked_without_consent(self) -> None:
        from agentic_trading.promotion import PromotionState, save_state

        with tempfile.TemporaryDirectory() as name:
            tmp = Path(name)
            loop, config = self._loop(tmp)
            save_state(config.state_dir, PromotionState(stage="probation"))
            with mock.patch.dict(os.environ, {"AGENTIC_ALLOW_AUTONOMY": "0"}):
                event = loop.reconcile_stage_mode()
        assert event is not None
        self.assertEqual(event["event"], "stage_mode_blocked")
        self.assertEqual(loop.mode, "shadow")
