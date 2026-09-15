"""Shadow runtime integration: journals would-place, never calls place."""

from __future__ import annotations

import json
import tempfile
import unittest
from datetime import date
from pathlib import Path

from agentic_trading.broker import Broker
from agentic_trading.config import load_config
from agentic_trading.runtime import run_loop
from agentic_trading.strategies.fixture import FixtureStrategy
from tests.fakes import FakeMcpClient

FIXTURES = Path(__file__).parent / "fixtures"
TOOLS_SNAPSHOT = FIXTURES / "tools_snapshot.json"
REPO_QUOTES = Path(__file__).resolve().parent.parent / "data" / "spy_quotes.jsonl"


def load_tools() -> list[dict]:
    payload = json.loads(TOOLS_SNAPSHOT.read_text(encoding="utf-8"))
    return payload["tools"]


def _write_config(tmp: Path, *, quotes_path: Path) -> Path:
    journal_dir = tmp / "journal"
    state_dir = tmp / "state"
    tools_path = tmp / "tools_snapshot.json"
    token_path = tmp / "missing-tokens.json"
    config_path = tmp / "agentic.toml"
    config_path.write_text(
        "\n".join(
            [
                'mode = "shadow"',
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


class ShadowRuntimeTests(unittest.TestCase):
    def test_shadow_runtime_journals_would_place_never_places(self) -> None:
        tools = load_tools()
        client = FakeMcpClient(tools)
        broker = Broker(client, tools)

        with tempfile.TemporaryDirectory() as tmp_name:
            tmp = Path(tmp_name)
            # Two quotes so FixtureStrategy can emit buy then sell
            quotes_path = tmp / "quotes.jsonl"
            quotes_path.write_text(
                "\n".join(
                    [
                        json.dumps(
                            {
                                "symbol": "SPY",
                                "observed_at": "2026-09-15T07:58:49Z",
                                "quote_at": "2026-09-15T07:58:42Z",
                                "bid": "100.00",
                                "ask": "100.10",
                            }
                        ),
                        json.dumps(
                            {
                                "symbol": "SPY",
                                "observed_at": "2026-09-15T07:59:49Z",
                                "quote_at": "2026-09-15T07:59:42Z",
                                "bid": "100.05",
                                "ask": "100.15",
                            }
                        ),
                    ]
                )
                + "\n",
                encoding="utf-8",
            )
            config_path = _write_config(tmp, quotes_path=quotes_path)
            config = load_config(config_path)

            run_loop(
                config,
                broker=broker,
                strategy=FixtureStrategy(),
                tools=tools,
            )

            journal_file = (
                Path(config.journal_dir) / f"{date.today().isoformat()}.jsonl"
            )
            self.assertTrue(journal_file.is_file())
            records = [
                json.loads(line)
                for line in journal_file.read_text(encoding="utf-8").splitlines()
                if line.strip()
            ]
            would_place = [r for r in records if r.get("would_place") is True]
            self.assertGreaterEqual(len(would_place), 1)
            accepted = [r for r in would_place if r.get("event") == "accepted"]
            self.assertGreaterEqual(len(accepted), 1)
            for record in accepted:
                intent = record["intent"]
                self.assertIn("quantity", intent)
                self.assertIn("ref_price", intent)
                self.assertIn("side", intent)
                self.assertIn("symbol", intent)

            place_calls = [c for c in client.calls if c.name == "place_equity_order"]
            self.assertEqual(len(place_calls), 0)

            review_calls = [c for c in client.calls if c.name == "review_equity_order"]
            self.assertGreaterEqual(len(review_calls), 1)

            self.assertTrue(Path(config.tools_snapshot_path).is_file())

    def test_shadow_runtime_with_repo_quotes_file(self) -> None:
        if not REPO_QUOTES.is_file():
            self.skipTest("data/spy_quotes.jsonl missing")

        tools = load_tools()
        client = FakeMcpClient(tools)
        broker = Broker(client, tools)

        with tempfile.TemporaryDirectory() as tmp_name:
            tmp = Path(tmp_name)
            config_path = _write_config(tmp, quotes_path=REPO_QUOTES)
            config = load_config(config_path)
            run_loop(
                config,
                broker=broker,
                strategy=FixtureStrategy(),
                tools=tools,
            )
            place_calls = [c for c in client.calls if c.name == "place_equity_order"]
            self.assertEqual(len(place_calls), 0)
            journal_file = (
                Path(config.journal_dir) / f"{date.today().isoformat()}.jsonl"
            )
            records = [
                json.loads(line)
                for line in journal_file.read_text(encoding="utf-8").splitlines()
                if line.strip()
            ]
            self.assertTrue(any(r.get("would_place") is True for r in records))


class CliSmokeTests(unittest.TestCase):
    def test_auth_stub_exits_2(self) -> None:
        from agentic_trading.cli import main

        code = main(["auth"])
        self.assertEqual(code, 2)

    def test_status_and_flip_mode(self) -> None:
        from agentic_trading.cli import main

        with tempfile.TemporaryDirectory() as tmp_name:
            tmp = Path(tmp_name)
            quotes_path = tmp / "quotes.jsonl"
            quotes_path.write_text("", encoding="utf-8")
            config_path = _write_config(tmp, quotes_path=quotes_path)
            self.assertEqual(
                main(["flip-mode", "live", "--config", str(config_path)]), 0
            )
            mode_file = tmp / "state" / "mode"
            self.assertEqual(mode_file.read_text(encoding="utf-8").strip(), "live")
            self.assertEqual(main(["status", "--config", str(config_path)]), 0)
            self.assertEqual(
                main(["reset-kill-switch", "--config", str(config_path)]), 0
            )


if __name__ == "__main__":
    unittest.main()
