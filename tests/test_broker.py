import json
from decimal import Decimal
from pathlib import Path
import tempfile
import unittest

from agentic_trading.broker import Broker
from agentic_trading.rh_mcp.snapshot import write_tools_snapshot
from agentic_trading.risk import PortfolioSnapshot

from tests.fakes import FakeMcpClient

FIXTURES = Path(__file__).parent / "fixtures"
TOOLS_SNAPSHOT = FIXTURES / "tools_snapshot.json"


def load_tools() -> list[dict]:
    payload = json.loads(TOOLS_SNAPSHOT.read_text(encoding="utf-8"))
    return payload["tools"]


class BrokerTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tools = load_tools()
        self.client = FakeMcpClient(self.tools)
        self.broker = Broker(self.client, self.tools)

    def test_get_equity(self) -> None:
        equity = self.broker.get_equity()
        self.assertEqual(equity, Decimal("1000"))
        self.assertEqual(len(self.client.calls), 1)
        self.assertEqual(self.client.calls[0].name, "get_account")

    def test_get_positions_empty(self) -> None:
        snap = self.broker.get_positions()
        self.assertEqual(snap, PortfolioSnapshot(open_positions=0, held={}))

    def test_review_calls_review_tool_not_place(self) -> None:
        result = self.broker.review_order(symbol="SPY", side="buy", quantity="1")
        self.assertTrue(result["ok"])
        self.assertEqual(len(self.client.calls), 1)
        self.assertEqual(self.client.calls[0].name, "review_equity_order")
        self.assertNotEqual(self.client.calls[0].name, "place_equity_order")

    def test_broker_place_is_callable_but_recorded(self) -> None:
        result = self.broker.place_order(symbol="SPY", side="buy", quantity="1")
        self.assertTrue(result["ok"])
        self.assertTrue(result["placed"])
        self.assertEqual(len(self.client.calls), 1)
        self.assertEqual(self.client.calls[0].name, "place_equity_order")
        self.assertEqual(
            self.client.calls[0].arguments,
            {"symbol": "SPY", "side": "buy", "quantity": "1"},
        )


class SnapshotTests(unittest.TestCase):
    def test_write_tools_snapshot(self) -> None:
        tools = load_tools()
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp) / "tools_snapshot.json"
            write_tools_snapshot(tools, out)
            self.assertTrue(out.exists())
            payload = json.loads(out.read_text(encoding="utf-8"))
            names = [t["name"] for t in payload["tools"]]
            self.assertEqual(
                names,
                ["review_equity_order", "get_account", "place_equity_order"],
            )

    def test_write_tools_snapshot_dated_copy(self) -> None:
        tools = load_tools()
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp) / "tools_snapshot.json"
            write_tools_snapshot(tools, out, dated_copy=True)
            dated_files = list(Path(tmp).glob("tools_snapshot_*.json"))
            self.assertEqual(len(dated_files), 1)
            payload = json.loads(dated_files[0].read_text(encoding="utf-8"))
            self.assertEqual(len(payload["tools"]), 3)
