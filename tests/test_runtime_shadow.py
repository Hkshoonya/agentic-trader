"""Shadow runtime integration: journals would-place, never calls place."""

from __future__ import annotations

import json
import os
import tempfile
import threading
import unittest
from datetime import date, datetime, timezone
from decimal import Decimal
from pathlib import Path
from unittest import mock

from agentic_trading.broker import Broker
from agentic_trading.config import load_config
from agentic_trading.runtime import run_loop
from agentic_trading.strategies.fixture import FixtureStrategy
from agentic_trading.types import OrderIntent, Side
from tests.fakes import FakeMcpClient

FIXTURES = Path(__file__).parent / "fixtures"
TOOLS_SNAPSHOT = FIXTURES / "tools_snapshot.json"
REPO_QUOTES = Path(__file__).resolve().parent.parent / "data" / "spy_quotes.jsonl"


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


class _StopOnReviewClient(FakeMcpClient):
    """Set stop_event after review so place must be skipped."""

    def __init__(self, tools: list[dict], stop_event: threading.Event) -> None:
        super().__init__(tools)
        self._stop_event = stop_event

    def call_tool(self, name: str, arguments: dict) -> dict:
        result = super().call_tool(name, arguments)
        if name == "review_equity_order":
            self._stop_event.set()
        return result


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

    def test_every_decision_carries_its_own_confidence(self) -> None:
        """`evidence` is one number for the strategy; `order` must be per order.

        The operator asked why the number beside each order never moved. It did
        not, because it is the strategy-level evidence grade. The per-order
        grade is what has to vary — and it must be present on records the
        strategy never got to trade, or a rejection cannot be explained.
        """
        tools = load_tools()
        client = FakeMcpClient(tools)
        broker = Broker(client, tools)

        with tempfile.TemporaryDirectory() as tmp_name:
            tmp = Path(tmp_name)
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
            records = [
                json.loads(line)
                for line in journal_file.read_text(encoding="utf-8").splitlines()
                if line.strip()
            ]

        decisions = [
            record
            for record in records
            if record.get("event") in ("accepted", "rejected", "place_failed")
        ]
        self.assertTrue(decisions, "the run must produce at least one decision")
        for record in decisions:
            confidence = record.get("confidence") or {}
            self.assertIn("evidence", confidence)
            self.assertIn("order", confidence, f"no per-order grade on {record}")
            order = confidence["order"]
            self.assertIn("score", order)
            self.assertIn(
                order["verdict"],
                ("buy", "marginal", "weak", "rejected", "unknown"),
            )
            self.assertIn("notes", order)

    def test_shadow_runtime_with_repo_quotes_file(self) -> None:
        """Premarket recording + fractional orders = not placeable (by design).

        Robinhood rejects fractional and dollar-based orders outside
        regular_hours, so the recorded premarket file must produce explicit
        ``order_invalid`` rejections instead of a would-place entry, and must
        never reach a place call.
        """
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
            self.assertFalse(any(r.get("would_place") is True for r in records))
            rejections = [r for r in records if r.get("event") == "rejected"]
            self.assertTrue(rejections)
            reasons = [str(r.get("reason", "")) for r in rejections]
            # The entry is unplaceable fractional premarket; any follow-up sell
            # then has no shadow position to close.
            self.assertTrue(
                any(reason.startswith("order_invalid") for reason in reasons), reasons
            )
            self.assertTrue(any(reason == "would_short" for reason in reasons), reasons)


class StopBeforePlaceTests(unittest.TestCase):
    def test_stop_after_review_skips_place(self) -> None:
        tools = load_tools()
        stop_event = threading.Event()
        client = _StopOnReviewClient(tools, stop_event)
        broker = Broker(client, tools)
        intent = OrderIntent(
            decision_id="stop-before-place",
            symbol="SPY",
            side=Side.BUY,
            quantity=Decimal("0.01"),
            ref_price=Decimal("100"),
            reason="live-stop-test",
            created_at=datetime(2026, 9, 15, tzinfo=timezone.utc),
        )

        with tempfile.TemporaryDirectory() as tmp_name:
            tmp = Path(tmp_name)
            quotes_path = tmp / "quotes.jsonl"
            quotes_path.write_text(
                json.dumps(
                    {
                        "symbol": "SPY",
                        "observed_at": "2026-09-15T07:58:49Z",
                        "quote_at": "2026-09-15T07:58:42Z",
                        "bid": "100.00",
                        "ask": "100.10",
                    }
                )
                + "\n",
                encoding="utf-8",
            )
            config_path = _write_config(tmp, quotes_path=quotes_path, mode="live")
            config = load_config(config_path)

            with mock.patch.dict(os.environ, {"AGENTIC_ALLOW_LIVE": "1"}):
                run_loop(
                    config,
                    broker=broker,
                    strategy=FixtureStrategy([intent]),
                    tools=tools,
                    stop_event=stop_event,
                )

            journal_file = (
                Path(config.journal_dir) / f"{date.today().isoformat()}.jsonl"
            )
            records = [
                json.loads(line)
                for line in journal_file.read_text(encoding="utf-8").splitlines()
                if line.strip()
            ]
            accepted = [r for r in records if r.get("event") == "accepted"]
            self.assertEqual(len(accepted), 1)
            self.assertTrue(accepted[0].get("would_place") is True)
            self.assertIn("review", accepted[0])

            place_calls = [c for c in client.calls if c.name == "place_equity_order"]
            self.assertEqual(len(place_calls), 0)
            review_calls = [c for c in client.calls if c.name == "review_equity_order"]
            self.assertEqual(len(review_calls), 1)


class CliSmokeTests(unittest.TestCase):
    def test_auth_requires_config(self) -> None:
        from agentic_trading.cli import main

        with self.assertRaises(SystemExit) as ctx:
            main(["auth"])
        self.assertEqual(ctx.exception.code, 2)

    def test_auth_invokes_oauth_with_config(self) -> None:
        from agentic_trading.cli import main

        with tempfile.TemporaryDirectory() as tmp_name:
            tmp = Path(tmp_name)
            quotes_path = tmp / "quotes.jsonl"
            quotes_path.write_text("", encoding="utf-8")
            config_path = _write_config(tmp, quotes_path=quotes_path)
            with mock.patch(
                "agentic_trading.cli.run_desktop_oauth",
                return_value=None,
            ) as oauth:
                code = main(["auth", "--config", str(config_path)])
            self.assertEqual(code, 0)
            oauth.assert_called_once()

    def test_snapshot_tools_requires_tokens(self) -> None:
        from agentic_trading.cli import main

        with tempfile.TemporaryDirectory() as tmp_name:
            tmp = Path(tmp_name)
            quotes_path = tmp / "quotes.jsonl"
            quotes_path.write_text("", encoding="utf-8")
            config_path = _write_config(tmp, quotes_path=quotes_path)
            code = main(["snapshot-tools", "--config", str(config_path)])
            self.assertEqual(code, 1)

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
