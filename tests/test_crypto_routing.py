"""Crypto orders must use the crypto MCP namespace, and only at stage=live.

The live crypto tools are a separate namespace from the equity ones: they take
the numeric ``rhs_account_number``, reject ``market_hours``, and spell the
stop-triggered market type ``stop_loss``. Routing a crypto order to
``review_equity_order`` fails with ``unexpected additional properties
["rhs_account_number"]``, which is what tripped the kill switch in production.
"""

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
from agentic_trading.orders import EquityOrderRequest, is_crypto_symbol
from agentic_trading.promotion import PromotionState, save_state
from agentic_trading.rh_mcp.snapshot import build_capability_map
from agentic_trading.runtime import _Loop, run_daemon
from agentic_trading.types import OrderIntent, Side
from tests.fakes import FakeMcpClient
from tests.schema import tool_schema, validate

FIXTURES = Path(__file__).parent / "fixtures"
TOOLS_SNAPSHOT = FIXTURES / "tools_snapshot.json"

FIXED_NOW = datetime(2026, 9, 16, 14, 0, 30, tzinfo=timezone.utc)
CRYPTO_ACCOUNT = "90000001"


def load_tools() -> list[dict]:
    return json.loads(TOOLS_SNAPSHOT.read_text(encoding="utf-8"))["tools"]


def _crypto_quote(second: int = 0) -> dict:
    # The crypto namespace returns pairs undashed; the live feed really does
    # produce BTCUSD, so the test fixture must too.
    return {
        "symbol": "BTCUSD",
        "observed_at": f"2026-09-16T14:00:{second:02d}Z",
        "quote_at": f"2026-09-16T13:59:{second:02d}Z",
        "bid": Decimal("118250.00"),
        "ask": Decimal("118280.00"),
    }


class _StubFeed:
    def __init__(self, quotes: list[dict]) -> None:
        self.quotes = list(quotes)

    def poll(self) -> list[dict]:
        pending, self.quotes = self.quotes, []
        return pending


class _CryptoBuy:
    """Emits a fractional BTC-USD entry for any quote it sees."""

    def on_quote(self, quote: dict) -> list[OrderIntent]:
        return [
            OrderIntent(
                decision_id=f"crypto-{quote['observed_at']}",
                symbol="BTC-USD",
                side=Side.BUY,
                quantity=Decimal("0.0002"),
                ref_price=quote["ask"],
                reason="test:crypto_buy",
                created_at=FIXED_NOW,
            )
        ]


def _write_config(tmp: Path, *, mode: str = "shadow", extra: list[str] | None = None) -> Path:
    config_path = tmp / "agentic.toml"
    lines = [
        f'mode = "{mode}"',
        'strategy = "fixture"',
        'symbol_whitelist = ["BTC-USD"]',
        'max_order_pct = "0.05"',
        'daily_notional_pct = "0.20"',
        'daily_loss_pct = "0.03"',
        "max_open_positions = 1",
        "equity_refresh_ticks = 30",
        "equity_refresh_seconds = 60",
        'timezone = "local"',
        'quote_source = "mcp"',
        'session_policy = "any"',
        'autonomy = "manual"',
        "evolution_interval_minutes = 0",
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


def _run(config, broker, *, stage: str = "shadow", feed=None, **kwargs: Any):
    save_state(config.state_dir, PromotionState(stage=stage))
    kwargs.setdefault("once", True)
    kwargs.setdefault("clock", lambda: FIXED_NOW)
    kwargs.setdefault("session_clock", lambda: "regular")
    return run_daemon(
        config,
        broker=broker,
        strategy=_CryptoBuy(),
        feed=feed or _StubFeed([_crypto_quote()]),
        **kwargs,
    )


class CryptoCapabilityTests(unittest.TestCase):
    def test_crypto_tools_map_to_their_own_capabilities(self) -> None:
        m = build_capability_map(load_tools())
        self.assertEqual(m["preview_crypto"], "preview_crypto_order")
        self.assertEqual(m["place_crypto"], "place_crypto_order")
        self.assertEqual(m["get_crypto_quotes"], "get_crypto_quotes")

    def test_crypto_tools_still_never_satisfy_equity_capabilities(self) -> None:
        m = build_capability_map(
            [{"name": "preview_crypto_order"}, {"name": "place_crypto_order"}]
        )
        self.assertNotIn("review_equity", m)
        self.assertNotIn("place_equity", m)

    def test_hyphen_and_usd_suffix_are_both_crypto(self) -> None:
        for symbol in ("BTC-USD", "BTCUSD", "eth-usd", "SOLUSD"):
            self.assertTrue(is_crypto_symbol(symbol), symbol)
        for symbol in ("SPY", "QQQ", "BRK.B"):
            self.assertFalse(is_crypto_symbol(symbol), symbol)


class CryptoOrderRoutingTests(unittest.TestCase):
    def _request(self, symbol: str) -> EquityOrderRequest:
        account = CRYPTO_ACCOUNT if is_crypto_symbol(symbol) else "FAKE0001"
        return EquityOrderRequest(
            account_number=account,
            symbol=symbol,
            side=Side.BUY,
            order_type="market",
            dollar_amount=Decimal("2.50"),
        )

    def test_crypto_review_uses_preview_crypto_order(self) -> None:
        tools = load_tools()
        client = FakeMcpClient(tools)
        broker = Broker(client, tools)

        broker.review_order(self._request("BTC-USD"))

        self.assertEqual(client.calls_named("review_equity_order"), [])
        calls = client.calls_named("preview_crypto_order")
        self.assertEqual(len(calls), 1)
        args = calls[0].arguments
        self.assertEqual(validate(tool_schema(tools, "preview_crypto_order"), args), [])
        self.assertEqual(args["rhs_account_number"], CRYPTO_ACCOUNT)
        self.assertNotIn("account_number", args)
        self.assertNotIn("market_hours", args)
        self.assertNotIn("ref_id", args)
        self.assertEqual(args["symbol"], "BTC-USD")
        self.assertEqual(args["type"], "market")

    def test_crypto_place_uses_place_crypto_order(self) -> None:
        tools = load_tools()
        client = FakeMcpClient(tools)
        broker = Broker(client, tools)

        broker.place_order(self._request("BTC-USD"))

        self.assertEqual(client.calls_named("place_equity_order"), [])
        calls = client.calls_named("place_crypto_order")
        self.assertEqual(len(calls), 1)
        self.assertEqual(validate(tool_schema(tools, "place_crypto_order"), calls[0].arguments), [])

    def test_equity_still_routes_to_the_equity_tools(self) -> None:
        tools = load_tools()
        client = FakeMcpClient(tools)
        broker = Broker(client, tools)

        broker.review_order(self._request("SPY"))
        broker.place_order(self._request("SPY"))

        self.assertEqual(client.calls_named("preview_crypto_order"), [])
        self.assertEqual(client.calls_named("place_crypto_order"), [])
        self.assertEqual(len(client.calls_named("review_equity_order")), 1)
        self.assertEqual(len(client.calls_named("place_equity_order")), 1)


class CryptoDaemonGateTests(unittest.TestCase):
    def test_decisions_record_the_confidence_they_were_taken_under(self) -> None:
        """The order table has to explain why a row was bought or rejected."""
        from agentic_trading.llm.advisor import AdvisorDecision

        broker, client = self._broker()
        with tempfile.TemporaryDirectory() as name:
            config = load_config(_write_config(Path(name)))
            loop = _Loop(config, broker, _CryptoBuy())
            loop.evidence_confidence = 0.5252

            class _Advisor:
                model = "fake-model"

                def review_entry(self, **_kwargs):
                    return AdvisorDecision(
                        action="allow", confidence=0.62, hold=False, reason="ok"
                    )

            loop.advisor = _Advisor()
            loop.start()  # the daemon resolves the account and equity first
            intent = _CryptoBuy().on_quote(_crypto_quote())[0]
            loop.process_intent(intent)

            records = [
                json.loads(line)
                for line in (
                    Path(config.journal_dir) / f"{date.today().isoformat()}.jsonl"
                ).read_text().splitlines()
                if line.strip()
            ]

        accepted = [r for r in records if r.get("event") == "accepted"]
        self.assertEqual(len(accepted), 1)
        confidence = accepted[0]["confidence"]
        self.assertEqual(confidence["evidence"], 0.5252)
        self.assertEqual(confidence["advisor"], 0.62)
        self.assertEqual(confidence["advisor_action"], "allow")
        self.assertEqual(confidence["advisor_model"], "fake-model")

    def test_live_positions_merge_the_crypto_book(self) -> None:
        tools = load_tools()
        client = FakeMcpClient(
            tools,
            equity="1000",
            positions=[{"symbol": "SPY", "quantity": "1"}],
            crypto_holdings={"BTC": "0.0015"},
        )
        broker = Broker(client, tools)
        with tempfile.TemporaryDirectory() as name:
            config = load_config(_write_config(Path(name), mode="live"))
            loop = _Loop(config, broker, _CryptoBuy())
            snapshot = loop.live_positions()

        self.assertEqual(snapshot.held["SPY"], Decimal("1"))
        self.assertEqual(snapshot.held["BTC-USD"], Decimal("0.0015"))
        self.assertEqual(snapshot.open_positions, 2)
        self.assertFalse(snapshot.positions_read_failed)

    def test_unreadable_crypto_book_fails_closed(self) -> None:
        tools = load_tools()

        class _Blind(FakeMcpClient):
            def call_tool(self, name: str, arguments: dict) -> dict:
                if name == "get_crypto_positions":
                    raise RuntimeError("crypto positions unavailable")
                return super().call_tool(name, arguments)

        broker = Broker(_Blind(tools), tools)
        with tempfile.TemporaryDirectory() as name:
            config = load_config(_write_config(Path(name), mode="live"))
            loop = _Loop(config, broker, _CryptoBuy())
            with self.assertRaises(RuntimeError):
                loop.live_positions()

    def test_a_held_crypto_position_blocks_another_entry(self) -> None:
        broker, client = self._broker()
        client.crypto_holdings = {"BTC": "0.0015"}
        with tempfile.TemporaryDirectory() as name:
            config = load_config(_write_config(Path(name), mode="live"))
            with mock.patch.dict(os.environ, {"AGENTIC_ALLOW_LIVE": "1"}):
                _run(config, broker, stage="live")

            self.assertEqual(client.calls_named("place_crypto_order"), [])
            reasons = [
                r.get("reason") for r in _records(config) if r.get("event") == "rejected"
            ]
            self.assertIn("max_open_positions", reasons)

    def test_a_pending_crypto_order_blocks_a_second_entry(self) -> None:
        broker, client = self._broker()
        client.crypto_orders = [
            {
                "id": "crypto-1",
                "currency_code": "BTC",
                "side": "buy",
                "state": "confirmed",
                "quantity": "0.0002",
            }
        ]
        with tempfile.TemporaryDirectory() as name:
            config = load_config(_write_config(Path(name), mode="live"))
            with mock.patch.dict(os.environ, {"AGENTIC_ALLOW_LIVE": "1"}):
                _run(config, broker, stage="live")

            self.assertEqual(client.calls_named("place_crypto_order"), [])
            records = _records(config)
            open_orders = [r for r in records if r.get("event") == "open_orders"]
            self.assertEqual(open_orders[0]["orders"][0]["symbol"], "BTC-USD")
            reasons = [r.get("reason") for r in records if r.get("event") == "rejected"]
            self.assertIn("open_order_pending", reasons)

    def _broker(self) -> tuple[Broker, FakeMcpClient]:
        tools = load_tools()
        client = FakeMcpClient(tools, equity="1000")
        return Broker(client, tools), client

    def test_shadow_cycle_reviews_crypto_and_never_places(self) -> None:
        broker, client = self._broker()
        with tempfile.TemporaryDirectory() as name:
            config = load_config(_write_config(Path(name)))
            _run(config, broker, stage="shadow")

            records = _records(config)
            accepted = [r for r in records if r.get("event") == "accepted"]
            self.assertEqual(len(accepted), 1)
            self.assertTrue(accepted[0]["would_place"])
            self.assertFalse(accepted[0]["may_place"])
            self.assertEqual(
                accepted[0]["order_request"]["rhs_account_number"], CRYPTO_ACCOUNT
            )
            self.assertEqual(client.calls_named("place_crypto_order"), [])
            self.assertEqual(client.calls_named("review_equity_order"), [])
            self.assertEqual(records[-1]["event"], "accepted")

    def test_live_below_live_stage_refuses_to_place_crypto(self) -> None:
        for stage in ("shadow", "probation"):
            with self.subTest(stage=stage):
                broker, client = self._broker()
                with tempfile.TemporaryDirectory() as name:
                    config = load_config(_write_config(Path(name), mode="live"))
                    with mock.patch.dict(os.environ, {"AGENTIC_ALLOW_LIVE": "1"}):
                        _run(config, broker, stage=stage)

                    self.assertEqual(client.calls_named("place_crypto_order"), [])
                    records = _records(config)
                    refused = [r for r in records if r.get("event") == "place_refused"]
                    self.assertEqual(len(refused), 1, records)
                    self.assertEqual(refused[0]["reason"], "crypto_requires_live_stage")
                    self.assertEqual(refused[0]["stage"], stage)

    def test_live_at_live_stage_places_a_schema_valid_crypto_order(self) -> None:
        tools = load_tools()
        client = FakeMcpClient(tools, equity="1000")
        broker = Broker(client, tools)

        with tempfile.TemporaryDirectory() as name:
            config = load_config(_write_config(Path(name), mode="live"))
            with mock.patch.dict(os.environ, {"AGENTIC_ALLOW_LIVE": "1"}):
                _run(config, broker, stage="live")

            calls = client.calls_named("place_crypto_order")
            self.assertEqual(len(calls), 1)
            self.assertEqual(
                validate(tool_schema(tools, "place_crypto_order"), calls[0].arguments),
                [],
            )
            self.assertEqual(calls[0].arguments["side"], "buy")
            self.assertEqual(calls[0].arguments["symbol"], "BTC-USD")
            self.assertNotIn("market_hours", calls[0].arguments)
            self.assertIn("ref_id", calls[0].arguments)
            self.assertTrue(any(r.get("event") == "placed" for r in _records(config)))

    def test_live_at_live_stage_without_env_gate_still_refuses(self) -> None:
        broker, client = self._broker()
        with tempfile.TemporaryDirectory() as name:
            config = load_config(_write_config(Path(name), mode="live"))
            env = {k: v for k, v in os.environ.items() if k != "AGENTIC_ALLOW_LIVE"}
            with mock.patch.dict(os.environ, env, clear=True):
                _run(config, broker, stage="live")
            self.assertEqual(client.calls_named("place_crypto_order"), [])
            records = _records(config)
            self.assertTrue(any(r.get("event") == "accepted" for r in records))
            # A proven-but-disarmed system must say so, not look like shadow.
            blocked = [r for r in records if r.get("event") == "live_gate_blocked"]
            self.assertEqual(len(blocked), 1, records)
            self.assertEqual(blocked[0]["reason"], "AGENTIC_ALLOW_LIVE_not_set")

    def test_live_gate_state_is_published_for_the_console(self) -> None:
        broker, _ = self._broker()
        with tempfile.TemporaryDirectory() as name:
            tmp = Path(name)
            config = load_config(_write_config(tmp, mode="live"))
            with mock.patch.dict(os.environ, {"AGENTIC_ALLOW_LIVE": "1"}):
                _run(config, broker, stage="live")
            gate = json.loads((Path(config.state_dir) / "live_gate.json").read_text())

        self.assertTrue(gate["allow_live"])
        self.assertEqual(gate["mode"], "live")
        self.assertIn("updated_at", gate)

    def test_disarmed_state_is_published_when_the_switch_is_absent(self) -> None:
        broker, _ = self._broker()
        with tempfile.TemporaryDirectory() as name:
            tmp = Path(name)
            config = load_config(_write_config(tmp, mode="live"))
            env = {k: v for k, v in os.environ.items() if k != "AGENTIC_ALLOW_LIVE"}
            with mock.patch.dict(os.environ, env, clear=True):
                _run(config, broker, stage="live")
            gate = json.loads((Path(config.state_dir) / "live_gate.json").read_text())

        self.assertFalse(gate["allow_live"])
        self.assertEqual(gate["stage"], "live")

    def test_open_orders_are_read_on_an_interval_not_every_cycle(self) -> None:
        """Each broker read is a ~1.3s round trip; don't pay it every poll."""
        broker, client = self._broker()
        with tempfile.TemporaryDirectory() as name:
            config = load_config(_write_config(Path(name), mode="live"))
            loop = _Loop(config, broker, _CryptoBuy())
            loop.note_open_orders()
            loop.note_open_orders()
            loop.note_open_orders()
            reads = len(client.calls_named("get_crypto_orders"))

        self.assertEqual(reads, 1)


if __name__ == "__main__":
    unittest.main()
