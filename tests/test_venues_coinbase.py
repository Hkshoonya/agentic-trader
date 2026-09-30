"""Coinbase adapters, driven entirely by fakes."""

from __future__ import annotations

import json
import unittest
from datetime import datetime, timezone
from decimal import Decimal

from agentic_trading.venues.coinbase import CoinbaseTicks, CoinbaseVenue, VenueError, ticker_to_ticks
from agentic_trading.venues.model import VenueOrder
from agentic_trading.venues.secrets import Credentials

T0 = datetime(2026, 9, 30, 15, 0, tzinfo=timezone.utc)
CREDS = Credentials("coinbase", "organizations/o/apiKeys/k1234", "-----BEGIN EC PRIVATE KEY-----\nX\n-----END EC PRIVATE KEY-----\n")


def _message(tickers, channel="ticker", timestamp="2026-09-30T14:59:59.123456789Z"):
    return json.dumps({"channel": channel, "timestamp": timestamp,
                       "events": [{"type": "update", "tickers": tickers}]})


BTC = {"product_id": "BTC-USD", "price": "65000.10", "best_bid": "65000.00", "best_ask": "65000.20"}


class TickerTests(unittest.TestCase):
    def test_a_ticker_message_becomes_ticks_with_nanosecond_time(self) -> None:
        ticks = ticker_to_ticks(_message([BTC, {**BTC, "product_id": "ETH-USD"}]), T0)
        self.assertEqual([t.symbol for t in ticks], ["BTC-USD", "ETH-USD"])
        self.assertEqual(ticks[0].bid, Decimal("65000.00"))
        self.assertEqual(ticks[0].exchange_at, datetime(2026, 9, 30, 14, 59, 59, 123456, tzinfo=timezone.utc))
        self.assertEqual(ticks[0].venue, "coinbase")

    def test_anything_else_is_dropped(self) -> None:
        for bad in ("not json", json.dumps(["list"]), _message([BTC], channel="heartbeats"),
                    _message([BTC], timestamp="soon"), _message([{"product_id": "BTC-USD"}]),
                    _message([{"price": "1"}]), json.dumps({"channel": "ticker", "events": "x"})):
            with self.subTest(bad=bad[:40]):
                self.assertEqual(ticker_to_ticks(bad, T0), [])

    def test_a_bad_field_drops_only_that_field(self) -> None:
        tick = ticker_to_ticks(_message([{**BTC, "best_bid": "abc"}]), T0)[0]
        self.assertIsNone(tick.bid)
        self.assertEqual(tick.ask, Decimal("65000.20"))


class FakeWS:
    def __init__(self, on_message, messages):
        self.on_message, self.messages = on_message, messages
        self.subscribed = None
        self.closed = False

    def open(self):
        pass

    def subscribe(self, product_ids, channels):
        self.subscribed = (tuple(product_ids), tuple(channels))

    def run_forever_with_exception_check(self):
        for message in self.messages:
            self.on_message(message)

    def close(self):
        self.closed = True


class StreamTests(unittest.TestCase):
    def test_the_stream_emits_valid_ticks_and_ignores_garbage(self) -> None:
        holder = {}

        def factory(creds, on_message):
            holder["ws"] = FakeWS(on_message, ["garbage", _message([BTC]), _message([BTC], channel="heartbeats")])
            return holder["ws"]

        source = CoinbaseTicks(CREDS, ["BTC-USD"], client_factory=factory, clock=lambda: T0)
        got = []
        source.run(got.append)
        self.assertEqual([t.symbol for t in got], ["BTC-USD"])
        self.assertEqual(holder["ws"].subscribed, (("BTC-USD",), ("ticker", "heartbeats")))
        self.assertEqual((source.key, source.venue, source.always_open), ("coinbase", "coinbase", True))
        source.stop()
        self.assertTrue(holder["ws"].closed)


class FakeREST:
    def __init__(self, success=True):
        self.calls = []
        self.success = success

    def get_accounts(self):
        return {"accounts": [
            {"currency": "USD", "available_balance": {"value": "40.5", "currency": "USD"}},
            {"currency": "USDC", "available_balance": {"value": "9.5", "currency": "USDC"}},
            {"currency": "BTC", "available_balance": {"value": "0.001", "currency": "BTC"}},
            {"currency": "ETH", "available_balance": {"value": "0", "currency": "ETH"}},
        ]}

    def market_order_buy(self, **kwargs):
        self.calls.append(("market_order_buy", kwargs))
        if not self.success:
            return {"success": False, "error_response": {"message": "INSUFFICIENT_FUND"}}
        return {"success": True, "success_response": {"order_id": "cb-1"}}

    def market_order_sell(self, **kwargs):
        self.calls.append(("market_order_sell", kwargs))
        return {"success": True, "success_response": {"order_id": "cb-2"}}

    def limit_order_gtc_buy(self, **kwargs):
        self.calls.append(("limit_order_gtc_buy", kwargs))
        return {"success": True, "success_response": {"order_id": "cb-3"}}

    def cancel_orders(self, order_ids):
        self.calls.append(("cancel_orders", order_ids))
        return {"results": [{"success": True}]}

    def list_orders(self, **kwargs):
        return {"orders": [{"order_id": "cb-9", "client_order_id": "at-9", "status": "OPEN",
                            "filled_size": "0", "average_filled_price": "0"}]}


class VenueTests(unittest.TestCase):
    def _venue(self, success=True):
        self.client = FakeREST(success)
        return CoinbaseVenue(CREDS, client_factory=lambda c: self.client)

    def test_cash_is_usd_plus_usdc_and_holdings_are_positions(self) -> None:
        venue = self._venue()
        account = venue.account()
        self.assertEqual((venue.name, venue.mode, account.cash), ("coinbase", "live", Decimal("50.0")))
        self.assertEqual([(p.symbol, p.qty) for p in venue.positions()], [("BTC-USD", Decimal("0.001"))])

    def test_a_market_buy_is_sized_in_dollars_and_carries_our_id(self) -> None:
        venue = self._venue()
        ack = venue.submit(VenueOrder("at-1", "BTC-USD", "buy", notional=Decimal("5")))
        name, kwargs = self.client.calls[0]
        self.assertEqual(name, "market_order_buy")
        self.assertEqual(kwargs, {"client_order_id": "at-1", "product_id": "BTC-USD", "quote_size": "5"})
        self.assertEqual(ack.venue_order_id, "cb-1")
        venue.cancel("at-1")
        self.assertEqual(self.client.calls[-1], ("cancel_orders", ["cb-1"]))

    def test_sizes_coinbase_cannot_take_are_refused_before_sending(self) -> None:
        venue = self._venue()
        with self.assertRaises(ValueError):
            venue.submit(VenueOrder("at-2", "BTC-USD", "buy", qty=Decimal("0.001")))
        with self.assertRaises(ValueError):
            venue.submit(VenueOrder("at-3", "BTC-USD", "sell", notional=Decimal("5")))
        self.assertEqual(self.client.calls, [])

    def test_a_refused_order_raises_with_coinbases_reason(self) -> None:
        with self.assertRaises(VenueError) as caught:
            self._venue(success=False).submit(VenueOrder("at-4", "BTC-USD", "buy", notional=Decimal("5")))
        self.assertIn("INSUFFICIENT_FUND", str(caught.exception))

    def test_open_orders_and_an_unknown_cancel(self) -> None:
        venue = self._venue()
        self.assertEqual(venue.open_orders()[0].client_order_id, "at-9")
        with self.assertRaises(KeyError):
            venue.cancel("never-sent")
