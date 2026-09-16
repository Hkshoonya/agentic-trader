"""Quote normalization and feed tests (fail-closed market data)."""

from __future__ import annotations

import json
import tempfile
import unittest
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path

from agentic_trading.marketdata import (
    FileQuoteFeed,
    McpQuoteFeed,
    QuoteShapeError,
    normalize_quotes_payload,
)

OBSERVED = datetime(2026, 9, 16, 14, 0, tzinfo=timezone.utc)


class NormalizeTests(unittest.TestCase):
    def test_list_payload_with_snake_case_fields(self) -> None:
        payload = {
            "quotes": [
                {
                    "symbol": "SPY",
                    "bid_price": "100.10",
                    "ask_price": "100.12",
                    "quote_time": "2026-09-16T13:59:58Z",
                }
            ]
        }
        quotes = normalize_quotes_payload(
            payload, symbols=["SPY"], observed_at=OBSERVED
        )
        self.assertEqual(len(quotes), 1)
        quote = quotes[0]
        self.assertEqual(quote["symbol"], "SPY")
        self.assertEqual(quote["bid"], Decimal("100.10"))
        self.assertEqual(quote["ask"], Decimal("100.12"))
        self.assertEqual(quote["quote_at"], "2026-09-16T13:59:58+00:00")
        self.assertEqual(quote["observed_at"], OBSERVED.isoformat())

    def test_symbol_keyed_mapping_payload(self) -> None:
        payload = {"quotes": {"spy": {"bid": "1.00", "ask": "1.02"}}}
        quotes = normalize_quotes_payload(
            payload, symbols=["SPY"], observed_at=OBSERVED
        )
        self.assertEqual(quotes[0]["symbol"], "SPY")

    def test_epoch_timestamp_is_accepted(self) -> None:
        payload = [
            {"symbol": "SPY", "bid": "1", "ask": "2", "timestamp": 1758031200}
        ]
        quotes = normalize_quotes_payload(
            payload, symbols=["SPY"], observed_at=OBSERVED
        )
        self.assertTrue(quotes[0]["quote_at"].startswith("2025-"))

    def test_quotes_without_two_sided_market_are_dropped(self) -> None:
        payload = [
            {"symbol": "SPY", "last_trade_price": "100.00"},
            {"symbol": "QQQ", "bid": "1.00"},
            {"symbol": "IWM", "bid": "2.00", "ask": "1.00"},  # crossed
        ]
        quotes = normalize_quotes_payload(
            payload, symbols=["SPY", "QQQ", "IWM"], observed_at=OBSERVED
        )
        self.assertEqual(quotes, [])

    def test_symbols_outside_the_request_are_ignored(self) -> None:
        payload = [
            {"symbol": "TSLA", "bid": "1", "ask": "2"},
        ]
        quotes = normalize_quotes_payload(
            payload, symbols=["SPY"], observed_at=OBSERVED
        )
        self.assertEqual(quotes, [])

    def test_unknown_shape_raises(self) -> None:
        with self.assertRaises(QuoteShapeError):
            normalize_quotes_payload(
                {"unexpected": "shape"}, symbols=["SPY"], observed_at=OBSERVED
            )
        with self.assertRaises(QuoteShapeError):
            normalize_quotes_payload("nope", symbols=["SPY"], observed_at=OBSERVED)

    def test_nested_amount_objects_are_accepted(self) -> None:
        payload = [
            {
                "symbol": "SPY",
                "bid": {"amount": "10.01"},
                "ask": {"amount": "10.03"},
            }
        ]
        quotes = normalize_quotes_payload(
            payload, symbols=["SPY"], observed_at=OBSERVED
        )
        self.assertEqual(quotes[0]["bid"], Decimal("10.01"))


class FileQuoteFeedTests(unittest.TestCase):
    def test_tails_only_new_complete_lines(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "quotes.jsonl"
            path.write_text(
                json.dumps(
                    {
                        "symbol": "SPY",
                        "observed_at": "2026-09-16T14:00:00Z",
                        "quote_at": "2026-09-16T13:59:59Z",
                        "bid": "1.00",
                        "ask": "1.02",
                    }
                )
                + "\n",
                encoding="utf-8",
            )
            feed = FileQuoteFeed(path)
            first = feed.poll()
            self.assertEqual(len(first), 1)
            self.assertEqual(feed.poll(), [])

            with path.open("a", encoding="utf-8") as handle:
                handle.write(
                    json.dumps(
                        {
                            "symbol": "SPY",
                            "observed_at": "2026-09-16T14:00:05Z",
                            "quote_at": "2026-09-16T14:00:04Z",
                            "bid": "1.05",
                            "ask": "1.07",
                        }
                    )
                    + "\n"
                )
            second = feed.poll()
            self.assertEqual(len(second), 1)
            self.assertEqual(second[0]["bid"], Decimal("1.05"))

    def test_partial_final_line_is_retried(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "quotes.jsonl"
            path.write_text(
                json.dumps(
                    {
                        "symbol": "SPY",
                        "observed_at": "2026-09-16T14:00:00Z",
                        "quote_at": "2026-09-16T13:59:59Z",
                        "bid": "1.00",
                        "ask": "1.02",
                    }
                ),
                encoding="utf-8",
            )
            feed = FileQuoteFeed(path)
            self.assertEqual(feed.poll(), [])
            with path.open("a", encoding="utf-8") as handle:
                handle.write("\n")
            self.assertEqual(len(feed.poll()), 1)

    def test_missing_file_yields_nothing(self) -> None:
        feed = FileQuoteFeed(Path("/nonexistent/quotes.jsonl"))
        self.assertEqual(feed.poll(), [])

    def test_first_poll_returns_existing_lines_then_only_new_ones(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "quotes.jsonl"
            path.write_text(
                json.dumps(
                    {
                        "symbol": "SPY",
                        "observed_at": "2026-09-15T14:00:00Z",
                        "quote_at": "2026-09-15T13:59:59Z",
                        "bid": "1.00",
                        "ask": "1.02",
                    }
                )
                + "\n",
                encoding="utf-8",
            )
            feed = FileQuoteFeed(path)
            self.assertEqual(len(feed.poll()), 1)

            with path.open("a", encoding="utf-8") as handle:
                handle.write(
                    json.dumps(
                        {
                            "symbol": "SPY",
                            "observed_at": "2026-09-16T14:00:00Z",
                            "quote_at": "2026-09-16T13:59:59Z",
                            "bid": "2.00",
                            "ask": "2.02",
                        }
                    )
                    + "\n"
                )
            fresh = feed.poll()
            self.assertEqual(len(fresh), 1)
            self.assertEqual(fresh[0]["bid"], Decimal("2.00"))


class McpQuoteFeedTests(unittest.TestCase):
    def test_polls_broker_once_per_call(self) -> None:
        class StubBroker:
            def __init__(self) -> None:
                self.requests: list[list[str]] = []

            def get_quotes(self, symbols: list[str]) -> dict:
                self.requests.append(list(symbols))
                return {
                    "quotes": [
                        {"symbol": "SPY", "bid_price": "1.00", "ask_price": "1.02"}
                    ]
                }

        broker = StubBroker()
        feed = McpQuoteFeed(broker, ["spy"], clock=lambda: OBSERVED)  # type: ignore[arg-type]
        quotes = feed.poll()
        self.assertEqual(broker.requests, [["SPY"]])
        self.assertEqual(quotes[0]["symbol"], "SPY")
        self.assertEqual(quotes[0]["observed_at"], OBSERVED.isoformat())


if __name__ == "__main__":
    unittest.main()
