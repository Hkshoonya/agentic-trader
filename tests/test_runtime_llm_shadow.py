"""Shadow runtime integration: LlmMultiAssetStrategy never places orders."""

from __future__ import annotations

import json
import tempfile
import unittest
from datetime import date
from pathlib import Path

from agentic_trading.broker import Broker
from agentic_trading.config import load_config
from agentic_trading.llm import FakeLlmClient
from agentic_trading.runtime import run_loop
from agentic_trading.strategies.llm_multi_asset import LlmMultiAssetStrategy
from tests.fakes import FakeMcpClient

FIXTURES = Path(__file__).parent / "fixtures"
TOOLS_SNAPSHOT = FIXTURES / "tools_snapshot.json"

BUY_SPY_JSON = (
    '{"intents":[{"symbol":"SPY","side":"buy","quantity":"0.01","reason":"llm:test"}]}'
)


def load_tools() -> list[dict]:
    payload = json.loads(TOOLS_SNAPSHOT.read_text(encoding="utf-8"))
    return payload["tools"]


def _write_config(tmp: Path, *, quotes_path: Path, mode: str = "shadow") -> Path:
    journal_dir = tmp / "journal"
    state_dir = tmp / "state"
    tools_path = tmp / "tools_snapshot.json"
    token_path = tmp / "missing-tokens.json"
    config_path = tmp / "agentic.toml"
    config_path.write_text(
        "\n".join(
            [
                f'mode = "{mode}"',
                'strategy = "llm"',
                'symbol_whitelist = ["SPY"]',
                'max_order_pct = "0.05"',
                'daily_notional_pct = "0.20"',
                'daily_loss_pct = "0.03"',
                "max_open_positions = 1",
                "equity_refresh_ticks = 30",
                "equity_refresh_seconds = 60",
                'timezone = "local"',
                f'quotes_path = "{quotes_path}"',
                f'journal_dir = "{journal_dir}"',
                f'state_dir = "{state_dir}"',
                f'tools_snapshot_path = "{tools_path}"',
                f'token_path = "{token_path}"',
                'mcp_url = "https://agent.robinhood.com/mcp/trading"',
                "",
            ]
        ),
        encoding="utf-8",
    )
    return config_path


def _synthetic_quotes() -> str:
    rows = [
        {
            "symbol": "SPY",
            "observed_at": "2026-09-15T12:00:00Z",
            "quote_at": "2026-09-15T11:59:59Z",
            "bid": "100.00",
            "ask": "100.02",
        },
        {
            "symbol": "SPY",
            "observed_at": "2026-09-15T12:00:05Z",
            "quote_at": "2026-09-15T12:00:04Z",
            "bid": "100.05",
            "ask": "100.07",
        },
    ]
    return "\n".join(json.dumps(row) for row in rows) + "\n"


class LlmMultiAssetShadowRuntimeTests(unittest.TestCase):
    def test_llm_shadow_never_places(self) -> None:
        tools = load_tools()
        client = FakeMcpClient(tools)
        broker = Broker(client, tools)

        with tempfile.TemporaryDirectory() as tmp_name:
            tmp = Path(tmp_name)
            quotes_path = tmp / "quotes.jsonl"
            quotes_path.write_text(_synthetic_quotes(), encoding="utf-8")
            config_path = _write_config(tmp, quotes_path=quotes_path)
            config = load_config(config_path)

            llm = FakeLlmClient(BUY_SPY_JSON)
            strategy = LlmMultiAssetStrategy(
                client=llm,
                whitelist=config.symbol_whitelist,
            )

            run_loop(
                config,
                broker=broker,
                strategy=strategy,
                tools=tools,
            )

            place_calls = [c for c in client.calls if c.name == "place_equity_order"]
            self.assertEqual(len(place_calls), 0)
            self.assertGreaterEqual(len(llm.calls), 1)

            journal_file = (
                Path(config.journal_dir) / f"{date.today().isoformat()}.jsonl"
            )
            self.assertTrue(journal_file.is_file())
            records = [
                json.loads(line)
                for line in journal_file.read_text(encoding="utf-8").splitlines()
                if line.strip()
            ]
            activity = [
                r
                for r in records
                if r.get("would_place") is True
                or r.get("event") in ("accepted", "rejected")
            ]
            self.assertGreaterEqual(len(activity), 1)


if __name__ == "__main__":
    unittest.main()
