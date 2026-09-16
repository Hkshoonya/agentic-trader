"""Broker contract tests against the recorded live MCP tool schema."""

from __future__ import annotations

import json
import tempfile
import unittest
from decimal import Decimal
from pathlib import Path

from agentic_trading.broker import Broker, BrokerPayloadError
from agentic_trading.orders import EquityOrderRequest
from agentic_trading.rh_mcp.snapshot import build_capability_map, write_tools_snapshot
from agentic_trading.risk import PortfolioSnapshot
from agentic_trading.types import Side

from tests.fakes import FakeMcpClient
from tests.schema import tool_schema, validate

FIXTURES = Path(__file__).parent / "fixtures"
TOOLS_SNAPSHOT = FIXTURES / "tools_snapshot.json"


def load_tools() -> list[dict]:
    payload = json.loads(TOOLS_SNAPSHOT.read_text(encoding="utf-8"))
    return payload["tools"]


def make_broker(**kwargs) -> tuple[Broker, FakeMcpClient]:
    tools = load_tools()
    client = FakeMcpClient(tools, **kwargs)
    return Broker(client, tools), client


class CapabilityMapTests(unittest.TestCase):
    def test_real_tool_names_map_to_capabilities(self) -> None:
        m = build_capability_map(load_tools())
        self.assertEqual(m["list_accounts"], "get_accounts")
        self.assertEqual(m["get_portfolio"], "get_portfolio")
        self.assertEqual(m["get_positions"], "get_equity_positions")
        self.assertEqual(m["get_quotes"], "get_equity_quotes")
        self.assertEqual(m["review_equity"], "review_equity_order")
        self.assertEqual(m["place_equity"], "place_equity_order")
        self.assertEqual(m["get_orders"], "get_equity_orders")
        self.assertEqual(m["cancel_equity"], "cancel_equity_order")

    def test_option_and_crypto_tools_never_map_to_equity(self) -> None:
        for name in (
            "place_option_order",
            "review_option_order",
            "get_crypto_orders",
            "get_crypto_positions",
        ):
            m = build_capability_map([{"name": name}])
            self.assertNotIn("place_equity", m, name)
            self.assertNotIn("review_equity", m, name)
            self.assertNotIn("get_orders", m, name)
            self.assertNotIn("list_accounts", m, name)

    def test_renamed_place_tool_still_maps(self) -> None:
        m = build_capability_map([{"name": "place_stock_order"}])
        self.assertEqual(m["place_equity"], "place_stock_order")

    def test_preview_account_is_not_an_accounts_tool(self) -> None:
        m = build_capability_map([{"name": "preview_account"}])
        self.assertNotIn("list_accounts", m)


class AccountAndValueTests(unittest.TestCase):
    def test_resolves_agentic_account_and_reads_equity(self) -> None:
        broker, client = make_broker(equity="250.50")
        self.assertEqual(broker.resolve_account_number(), "FAKE0001")
        self.assertEqual(broker.get_equity(), Decimal("250.50"))
        self.assertEqual(broker.last_equity_source, "total_equity")
        self.assertEqual(client.calls_named("get_accounts")[0].arguments, {})
        self.assertEqual(
            client.calls_named("get_portfolio")[0].arguments,
            {"account_number": "FAKE0001"},
        )

    def test_configured_account_skips_discovery(self) -> None:
        tools = load_tools()
        client = FakeMcpClient(tools)
        broker = Broker(client, tools, account_number="PINNED")
        self.assertEqual(broker.resolve_account_number(), "PINNED")
        self.assertEqual(client.calls, [])

    def test_no_agentic_account_is_an_error(self) -> None:
        broker, _ = make_broker(agentic_allowed=False)
        with self.assertRaises(BrokerPayloadError):
            broker.resolve_account_number()

    def test_unknown_portfolio_shape_fails_closed(self) -> None:
        tools = load_tools()
        client = FakeMcpClient(tools)
        client.call_tool = lambda name, args: {"market_value": "10"}  # type: ignore[assignment]
        broker = Broker(client, tools)
        with self.assertRaises(BrokerPayloadError):
            broker.get_equity()


class PositionTests(unittest.TestCase):
    def test_positions_parsed_from_results(self) -> None:
        broker, _ = make_broker(
            positions=[{"symbol": "SPY", "quantity": "1.5"}, {"symbol": "QQQ", "qty": "2"}]
        )
        snap = broker.get_positions()
        self.assertEqual(
            snap, PortfolioSnapshot(open_positions=2, held={"SPY": Decimal("1.5"), "QQQ": Decimal("2")})
        )

    def test_zero_quantity_positions_ignored(self) -> None:
        broker, _ = make_broker(positions=[{"symbol": "SPY", "quantity": "0"}])
        self.assertEqual(broker.get_positions().held, {})

    def test_missing_list_marks_read_failed(self) -> None:
        tools = load_tools()
        client = FakeMcpClient(tools)
        client.call_tool = lambda name, args: {"unexpected": True}  # type: ignore[assignment]
        broker = Broker(client, tools, account_number="X")
        snap = broker.get_positions()
        self.assertTrue(snap.positions_read_failed)
        self.assertEqual(snap.held, {})


class OrderArgumentTests(unittest.TestCase):
    """Every order the broker sends must satisfy the recorded tool schema."""

    def _request(self, **overrides) -> EquityOrderRequest:
        base = {
            "account_number": "FAKE0001",
            "symbol": "SPY",
            "side": Side.BUY,
            "order_type": "market",
            "dollar_amount": Decimal("25.00"),
            "market_hours": "regular_hours",
        }
        base.update(overrides)
        return EquityOrderRequest(**base)  # type: ignore[arg-type]

    def test_place_arguments_match_place_schema(self) -> None:
        tools = load_tools()
        broker, client = make_broker()
        request = self._request(ref_id="decision-123")
        broker.place_order(request)
        call = client.calls_named("place_equity_order")[0]
        self.assertEqual(
            validate(tool_schema(tools, "place_equity_order"), call.arguments), []
        )
        self.assertEqual(call.arguments["ref_id"], "decision-123")
        self.assertEqual(call.arguments["dollar_amount"], "25.00")

    def test_review_arguments_match_review_schema(self) -> None:
        tools = load_tools()
        broker, client = make_broker()
        broker.review_order(
            self._request(
                order_type="limit",
                dollar_amount=None,
                quantity=Decimal("1"),
                limit_price=Decimal("100.25"),
            )
        )
        call = client.calls_named("review_equity_order")[0]
        self.assertEqual(
            validate(tool_schema(tools, "review_equity_order"), call.arguments), []
        )

    def test_position_arguments_match_quotes_schema(self) -> None:
        tools = load_tools()
        broker, client = make_broker()
        broker.get_quotes(["spy", "qqq"])
        call = client.calls_named("get_equity_quotes")[0]
        self.assertEqual(
            validate(tool_schema(tools, "get_equity_quotes"), call.arguments), []
        )
        self.assertEqual(call.arguments["symbols"], ["SPY", "QQQ"])

    def test_order_lookup_arguments_match_schema(self) -> None:
        tools = load_tools()
        broker, client = make_broker()
        broker.get_orders(state_group="open")
        call = client.calls_named("get_equity_orders")[0]
        self.assertEqual(
            validate(tool_schema(tools, "get_equity_orders"), call.arguments), []
        )

    def test_quotes_reject_more_than_20_symbols(self) -> None:
        broker, _ = make_broker()
        with self.assertRaises(ValueError):
            broker.get_quotes([f"S{i}" for i in range(21)])


class SnapshotTests(unittest.TestCase):
    def test_write_tools_snapshot(self) -> None:
        tools = load_tools()
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp) / "tools_snapshot.json"
            write_tools_snapshot(tools, out)
            payload = json.loads(out.read_text(encoding="utf-8"))
            self.assertEqual(len(payload["tools"]), len(tools))
            self.assertEqual(
                payload["capability_map"]["place_equity"], "place_equity_order"
            )

    def test_write_tools_snapshot_dated_copy(self) -> None:
        tools = load_tools()
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp) / "tools_snapshot.json"
            write_tools_snapshot(tools, out, dated_copy=True)
            self.assertEqual(len(list(Path(tmp).glob("tools_snapshot_*.json"))), 1)


if __name__ == "__main__":
    unittest.main()
