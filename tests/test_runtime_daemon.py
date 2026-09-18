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
    tmp: Path,
    *,
    mode: str = "shadow",
    extra: list[str] | None = None,
    max_order_pct: str = "0.05",
) -> Path:
    config_path = tmp / "agentic.toml"
    lines = [
        f'mode = "{mode}"',
        'symbol_whitelist = ["SPY"]',
        f'max_order_pct = "{max_order_pct}"',
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


class StartupResilienceTests(unittest.TestCase):
    """A network blip must not stop the agent for hours.

    On 2026-09-18 a DNS failure at startup raised straight out of run_daemon and
    the service crash-looped 419 times over six and a half hours. Two bugs made
    that possible: the failure was not retried, and the shutdown path masked the
    real error with an UnboundLocalError.
    """

    def test_a_network_failure_is_retried_until_it_recovers(self) -> None:
        from agentic_trading import runtime as module

        attempts = {"n": 0}
        waits: list[float] = []

        class _Flaky:
            journal = mock.Mock()

            def start(self) -> None:
                attempts["n"] += 1
                if attempts["n"] < 3:
                    raise OSError("[Errno -2] Name or service not known")

        module._start_with_retry(_Flaky(), sleep=waits.append, clock=lambda: 0.0)
        self.assertEqual(attempts["n"], 3)
        self.assertEqual(waits, [5.0, 10.0], "backoff must grow between attempts")

    def test_giving_up_raises_the_real_error_not_a_masked_one(self) -> None:
        from agentic_trading import runtime as module

        class _Down:
            journal = mock.Mock()

            def start(self) -> None:
                raise OSError("Name or service not known")

        clock = iter([0.0, 0.0, 9999.0, 9999.0])
        with self.assertRaises(OSError) as caught:
            module._start_with_retry(
                _Down(), sleep=lambda _: None, clock=lambda: next(clock)
            )
        self.assertIn("Name or service", str(caught.exception))

    def test_a_configuration_error_fails_fast(self) -> None:
        """Waiting forever on a mistake wastes the outage and hides it."""
        from agentic_trading import runtime as module

        class _BadConfig:
            journal = mock.Mock()

            def start(self) -> None:
                raise FileNotFoundError("no tokens.json")

        with self.assertRaises(FileNotFoundError):
            module._start_with_retry(_BadConfig(), sleep=lambda _: None)

    def test_retryable_classification(self) -> None:
        from agentic_trading import runtime as module

        for name in ("ConnectError", "ReadTimeout", "gaierror", "SSLError"):
            exc = type(name, (Exception,), {})("down")
            self.assertTrue(module._is_retryable_startup_error(exc), name)
        for exc in (FileNotFoundError("x"), ValueError("x"), TypeError("x")):
            self.assertFalse(module._is_retryable_startup_error(exc), type(exc).__name__)

    def test_the_daemon_joins_workers_even_when_startup_fails(self) -> None:
        """The finally block must not reference a name the try never bound."""
        from agentic_trading import runtime as module

        tools = load_tools()
        client = FakeMcpClient(tools)
        broker = Broker(client, tools)
        with tempfile.TemporaryDirectory() as name:
            tmp = Path(name)
            config_path = _write_config(tmp, mode="shadow", extra=['autonomy = "manual"'])
            config = load_config(config_path)
            boom = RuntimeError("startup exploded")
            with mock.patch(
                "agentic_trading.runtime._start_with_retry", side_effect=boom
            ):
                with self.assertRaises(RuntimeError) as caught:
                    module.run_daemon(
                        config,
                        broker=broker,
                        strategy=FixtureStrategy(),
                        tools=tools,
                        once=True,
                        sleep=lambda _: None,
                    )
        # The original error, not UnboundLocalError about `workers`.
        self.assertIn("startup exploded", str(caught.exception))


class StartupBannerTests(unittest.TestCase):
    """`tail daemon.log` must show the present, not a crash from hours ago.

    The daemon writes to the journal when healthy and to stdout only when
    something goes wrong, so without a banner the log's last lines are the last
    failure — which reads as "it is still broken" long after it was fixed.
    """

    def test_the_daemon_announces_its_start_with_a_timestamp(self) -> None:
        from agentic_trading import runtime as module
        from unittest import mock
        import io
        import contextlib

        tools = load_tools()
        client = FakeMcpClient(tools)
        broker = Broker(client, tools)
        with tempfile.TemporaryDirectory() as name:
            tmp = Path(name)
            config_path = _write_config(
                tmp, mode="shadow", extra=['autonomy = "manual"']
            )
            config = load_config(config_path)
            buffer = io.StringIO()
            with mock.patch(
                "agentic_trading.runtime._start_with_retry"
            ), contextlib.redirect_stdout(buffer):
                module.run_daemon(
                    config,
                    broker=broker,
                    strategy=FixtureStrategy(),
                    tools=tools,
                    once=True,
                    sleep=lambda _: None,
                )
        output = buffer.getvalue()
        self.assertIn("starting:", output)
        self.assertIn("mode=", output)
        self.assertIn("started:", output)
        # A banner without a date cannot answer "when did this last start?".
        self.assertRegex(output, r"\[\d{4}-\d{2}-\d{2}T")


class SmallAccountModeTests(unittest.TestCase):
    """The operator can authorise a bigger temporary bet on a small account.

    Without it, a $50 account refuses every entry: 0.92% of $50 is $0.46 and the
    broker minimum is $1.00. The rule has to raise the cap *just* enough, say so
    in the journal, and release it as soon as the account can stand on its own.
    """

    def _loop(self, tmp: Path, *, floor: str, equity: str = "50"):
        from agentic_trading.runtime import _Loop

        # A 1% cap is the real situation: $0.50 of a $50 account, below the
        # $1.00 minimum. The default 5% test cap would need no floor at all.
        config_path = _write_config(
            tmp,
            mode="shadow",
            max_order_pct="0.01",
            extra=['autonomy = "manual"', f'small_account_max_order_pct = "{floor}"'],
        )
        config = load_config(config_path)
        client = FakeMcpClient(load_tools())
        broker = Broker(client, load_tools())
        (tmp / "state").mkdir(parents=True, exist_ok=True)
        loop = _Loop(config, broker, FixtureStrategy())
        loop.guard.update_equity(Decimal(equity))
        return loop, config

    def test_engaging_is_journalled_once_with_both_caps(self) -> None:
        with tempfile.TemporaryDirectory() as name:
            tmp = Path(name)
            loop, config = self._loop(tmp, floor="0.025")
            first = loop.apply_size_floor()
            again = loop.apply_size_floor()
        self.assertIsNotNone(first)
        assert first is not None
        self.assertTrue(first["engaged"])
        self.assertTrue(first["sufficient"])
        self.assertEqual(first["policy_max_order_pct"], "0.01")
        # 1.02 / 50: the minimum plus the rounding margin, inside the authorised
        # 2.5% ceiling.
        self.assertEqual(first["effective_max_order_pct"], "0.0204")
        self.assertIsNone(again, "an unchanged state must not spam the journal")
        self.assertEqual(Decimal(loop.guard.max_order_pct), Decimal("0.0204"))

    def test_the_disabled_default_changes_nothing(self) -> None:
        """Off means off: without authorisation the guard keeps its own cap."""
        with tempfile.TemporaryDirectory() as name:
            tmp = Path(name)
            loop, _ = self._loop(tmp, floor="0")
            before = Decimal(loop.guard.max_order_pct)
            self.assertIsNone(loop.apply_size_floor())
        self.assertEqual(Decimal(loop.guard.max_order_pct), before)

    def test_it_releases_the_cap_when_the_account_grows(self) -> None:
        with tempfile.TemporaryDirectory() as name:
            tmp = Path(name)
            loop, _ = self._loop(tmp, floor="0.025")
            loop.apply_size_floor()
            self.assertGreater(Decimal(loop.guard.max_order_pct), Decimal("0.01"))
            loop.guard.update_equity(Decimal("500"))
            released = loop.apply_size_floor()
        assert released is not None
        self.assertFalse(released["engaged"])
        self.assertEqual(Decimal(loop.guard.max_order_pct), Decimal("0.01"))

    def test_an_insufficient_ceiling_is_reported_not_hidden(self) -> None:
        """A ceiling of exactly the need rounds under the minimum at $50."""
        with tempfile.TemporaryDirectory() as name:
            tmp = Path(name)
            loop, _ = self._loop(tmp, floor="0.02")
            event = loop.apply_size_floor()
        assert event is not None
        self.assertTrue(event["engaged"])
        self.assertFalse(event["sufficient"])
        self.assertIn("raise small_account_max_order_pct", event["reason"])

    def test_the_refused_entry_becomes_a_placeable_order(self) -> None:
        """End to end: the intent that produced `below_min_notional` is sized."""
        from agentic_trading.types import OrderIntent, Side

        with tempfile.TemporaryDirectory() as name:
            tmp = Path(name)
            loop, _ = self._loop(tmp, floor="0.025")
            loop.apply_size_floor()
            intent = OrderIntent(
                decision_id="floor-1",
                symbol="BCH-USD",
                side=Side.BUY,
                quantity=Decimal("1"),
                ref_price=Decimal("233.85"),
                reason="trend_entry",
                created_at=datetime(2026, 9, 18, tzinfo=timezone.utc),
            )
            loop.process_intent(intent)
            records = _records(loop.config)
            names = [record.get("event") for record in records]
            rejected = [
                record
                for record in records
                if record.get("event") == "rejected"
                and record.get("reason") == "below_min_notional"
            ]
            resized = [record for record in records if record.get("event") == "resized"]
        self.assertEqual(rejected, [], "the floor must remove the refusal")
        self.assertIn("resized", names)
        self.assertTrue(resized)
        self.assertGreaterEqual(
            Decimal(resized[0]["to_quantity"]) * Decimal("233.85"), Decimal("1")
        )
