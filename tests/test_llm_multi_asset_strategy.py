"""Unit tests for FakeLlmClient + LlmMultiAssetStrategy."""

from __future__ import annotations

import unittest
from decimal import Decimal

from agentic_trading.llm import FakeLlmClient, build_llm_client
from agentic_trading.strategies.llm_multi_asset import LlmMultiAssetStrategy
from agentic_trading.types import Side


def _quote(
    *,
    symbol: str = "SPY",
    bid: str = "100.00",
    ask: str = "100.02",
    observed_at: str = "2026-09-15T12:00:00Z",
) -> dict:
    return {
        "symbol": symbol,
        "bid": bid,
        "ask": ask,
        "observed_at": observed_at,
    }


BUY_SPY_JSON = (
    '{"intents":[{"symbol":"SPY","side":"buy","quantity":"0.01","reason":"llm:test"}]}'
)


class LlmMultiAssetStrategyTests(unittest.TestCase):
    def test_fake_llm_buy_spy(self) -> None:
        client = FakeLlmClient(BUY_SPY_JSON)
        strategy = LlmMultiAssetStrategy(
            client=client,
            whitelist=frozenset({"SPY"}),
            default_quantity=Decimal("0.01"),
        )
        intents = strategy.on_quote(_quote())
        self.assertEqual(len(intents), 1)
        buy = intents[0]
        self.assertEqual(buy.side, Side.BUY)
        self.assertEqual(buy.symbol, "SPY")
        self.assertEqual(buy.quantity, Decimal("0.01"))
        self.assertEqual(buy.ref_price, Decimal("100.02"))
        self.assertEqual(buy.reason, "llm:test")
        self.assertEqual(len(client.calls), 1)

    def test_wrong_symbol_rejected(self) -> None:
        client = FakeLlmClient(
            '{"intents":[{"symbol":"TSLA","side":"buy","quantity":"0.01","reason":"x"}]}'
        )
        strategy = LlmMultiAssetStrategy(
            client=client, whitelist=frozenset({"SPY", "QQQ"})
        )
        intents = strategy.on_quote(_quote(symbol="SPY"))
        self.assertEqual(intents, [])
        self.assertEqual(len(client.calls), 1)

    def test_quote_symbol_outside_whitelist_skips_llm(self) -> None:
        client = FakeLlmClient(BUY_SPY_JSON)
        strategy = LlmMultiAssetStrategy(client=client, whitelist=frozenset({"SPY"}))
        intents = strategy.on_quote(_quote(symbol="QQQ"))
        self.assertEqual(intents, [])
        self.assertEqual(len(client.calls), 0)

    def test_malformed_json_returns_empty(self) -> None:
        client = FakeLlmClient("not-json{{{")
        strategy = LlmMultiAssetStrategy(client=client, whitelist=frozenset({"SPY"}))
        self.assertEqual(strategy.on_quote(_quote()), [])

    def test_empty_intents_returns_empty(self) -> None:
        client = FakeLlmClient('{"intents":[]}')
        strategy = LlmMultiAssetStrategy(client=client, whitelist=frozenset({"SPY"}))
        self.assertEqual(strategy.on_quote(_quote()), [])

    def test_missing_quantity_returns_empty(self) -> None:
        client = FakeLlmClient(
            '{"intents":[{"symbol":"SPY","side":"buy","reason":"x"}]}'
        )
        strategy = LlmMultiAssetStrategy(client=client, whitelist=frozenset({"SPY"}))
        self.assertEqual(strategy.on_quote(_quote()), [])

    def test_sell_uses_bid_as_ref_price(self) -> None:
        client = FakeLlmClient(
            '{"intents":[{"symbol":"SPY","side":"sell","quantity":"0.01","reason":"llm:exit"}]}'
        )
        strategy = LlmMultiAssetStrategy(client=client, whitelist=frozenset({"SPY"}))
        intents = strategy.on_quote(_quote())
        self.assertEqual(len(intents), 1)
        self.assertEqual(intents[0].side, Side.SELL)
        self.assertEqual(intents[0].ref_price, Decimal("100.00"))

    def test_build_llm_client_without_key_returns_fake(self) -> None:
        client = build_llm_client(api_key="")
        self.assertIsInstance(client, FakeLlmClient)
        self.assertEqual(client.complete("s", "u"), '{"intents":[]}')


if __name__ == "__main__":
    unittest.main()
