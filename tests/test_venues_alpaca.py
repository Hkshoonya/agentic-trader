"""Alpaca adapters, driven entirely by fakes: no test touches the network."""

from __future__ import annotations

import asyncio
import importlib.util
import unittest
from datetime import datetime, timezone
from decimal import Decimal
from types import SimpleNamespace as NS

from agentic_trading.venues.model import VenueOrder
from agentic_trading.venues.secrets import Credentials

HAVE_ALPACA = importlib.util.find_spec("alpaca") is not None
T0 = datetime(2026, 9, 30, 15, 0, tzinfo=timezone.utc)
CREDS = Credentials("alpaca_paper", "PKTESTKEY0000000WXYZ", "papersecret-0000000000")


class FakeStream:
    """Replays scripted quote and trade objects through the handlers, then returns."""

    def __init__(self, quotes=(), trades=()):
        self.quotes, self.trades = list(quotes), list(trades)
        self.quote_handler = self.trade_handler = None
        self.subscribed: tuple = ()
        self.stopped = False

    def subscribe_quotes(self, handler, *symbols):
        self.quote_handler, self.subscribed = handler, symbols

    def subscribe_trades(self, handler, *symbols):
        self.trade_handler = handler

    def run(self):
        for quote in self.quotes:
            asyncio.run(self.quote_handler(quote))
        for trade in self.trades:
            asyncio.run(self.trade_handler(trade))

    def stop(self):
        self.stopped = True


def _quote(symbol="SPY", bid=500.01, ask=500.03, at=T0):
    return NS(symbol=symbol, bid_price=bid, ask_price=ask, bid_size=100, ask_size=100, timestamp=at)


def _trade(symbol="SPY", price=500.02, size=10, at=T0):
    return NS(symbol=symbol, price=price, size=size, timestamp=at)


class ConversionTests(unittest.TestCase):
    def test_a_quote_becomes_a_tick(self) -> None:
        from agentic_trading.venues.alpaca import quote_to_tick

        tick = quote_to_tick(_quote(), "alpaca", T0)
        self.assertEqual((tick.symbol, tick.bid, tick.ask), ("SPY", Decimal("500.01"), Decimal("500.03")))
        self.assertEqual(tick.venue, "alpaca")

    def test_malformed_quotes_and_trades_are_dropped(self) -> None:
        from agentic_trading.venues.alpaca import quote_to_tick, trade_to_tick

        for bad in (_quote(bid=None, ask=None), _quote(bid=0, ask=-1), _quote(bid="abc", ask=None),
                    _quote(symbol=None), _quote(at=None)):
            with self.subTest(bad=bad):
                self.assertIsNone(quote_to_tick(bad, "alpaca", T0))
        for bad in (_trade(price=None), _trade(price=0), _trade(price="nan"), _trade(at="yesterday")):
            with self.subTest(bad=bad):
                self.assertIsNone(trade_to_tick(bad, "alpaca", T0))

    def test_a_naive_timestamp_is_read_as_utc(self) -> None:
        from agentic_trading.venues.alpaca import trade_to_tick

        tick = trade_to_tick(_trade(at=datetime(2026, 9, 30, 15, 0)), "alpaca", T0)
        self.assertEqual(tick.exchange_at, T0)
        self.assertEqual(tick.last, Decimal("500.02"))


class StreamTests(unittest.TestCase):
    def test_the_stream_emits_good_ticks_and_survives_bad_ones(self) -> None:
        from agentic_trading.venues.alpaca import AlpacaStockTicks

        fake = FakeStream(quotes=[_quote(), _quote(bid=None, ask=None), _quote("QQQ")],
                          trades=[_trade()])
        source = AlpacaStockTicks(CREDS, ["SPY", "QQQ"], stream_factory=lambda c: fake,
                                  clock=lambda: T0)
        got = []
        source.run(got.append)
        self.assertEqual([(t.symbol, t.last is None) for t in got],
                         [("SPY", True), ("QQQ", True), ("SPY", False)])
        self.assertEqual(fake.subscribed, ("SPY", "QQQ"))
        self.assertEqual((source.key, source.venue, source.always_open), ("alpaca_stocks", "alpaca", False))
        source.stop()
        self.assertTrue(fake.stopped)

    def test_an_emit_failure_does_not_kill_the_stream(self) -> None:
        from agentic_trading.venues.alpaca import AlpacaCryptoTicks

        fake = FakeStream(quotes=[_quote("BTC/USD"), _quote("ETH/USD")])
        source = AlpacaCryptoTicks(CREDS, ["BTC/USD", "ETH/USD"], stream_factory=lambda c: fake,
                                   clock=lambda: T0)
        seen = []

        def emit(tick):
            seen.append(tick.symbol)
            raise RuntimeError("subscriber bug")

        source.run(emit)
        self.assertEqual(seen, ["BTC/USD", "ETH/USD"])
        self.assertTrue(source.always_open)


class FakeTradingClient:
    def __init__(self):
        self.submitted = []
        self.cancelled = []
        self.orders = {"at-1": NS(id="venue-1", client_order_id="at-1", status=NS(value="new"),
                                  filled_qty="0", filled_avg_price=None)}

    def get_account(self):
        return NS(equity="100000.5", cash="90000", buying_power="180000", daytrade_count=2,
                  pattern_day_trader=False)

    def get_all_positions(self):
        return [NS(symbol="SPY", qty="2", market_value="1000.4")]

    def get_orders(self, filter=None):
        return list(self.orders.values())

    def submit_order(self, order_data):
        self.submitted.append(order_data)
        return NS(id="venue-9", client_order_id=order_data.client_order_id,
                  status=NS(value="accepted"), filled_qty="0", filled_avg_price=None)

    def get_order_by_client_id(self, client_id):
        return self.orders[client_id]

    def cancel_order_by_id(self, order_id):
        self.cancelled.append(order_id)


@unittest.skipUnless(HAVE_ALPACA, "alpaca-py is not installed")
class VenueTests(unittest.TestCase):
    def _venue(self, paper=True):
        from agentic_trading.venues.alpaca import AlpacaVenue

        self.client = FakeTradingClient()
        return AlpacaVenue(CREDS, paper=paper, client_factory=lambda c, p: self.client)

    def test_account_and_positions_map_to_views(self) -> None:
        venue = self._venue()
        account = venue.account()
        self.assertEqual((venue.name, venue.mode), ("alpaca_paper", "paper"))
        self.assertEqual((account.equity, account.day_trades), (Decimal("100000.5"), 2))
        self.assertEqual(venue.positions()[0].market_value, Decimal("1000.4"))
        self.assertEqual(self._venue(paper=False).name, "alpaca_live")

    def test_a_dollar_order_on_a_stock_is_a_day_market_order_with_our_id(self) -> None:
        venue = self._venue()
        ack = venue.submit(VenueOrder("at-7", "SPY", "buy", notional=Decimal("1")))
        request = self.client.submitted[0]
        self.assertEqual((request.symbol, request.notional, request.client_order_id), ("SPY", 1.0, "at-7"))
        self.assertEqual(str(getattr(request.time_in_force, "value", request.time_in_force)), "day")
        self.assertEqual((ack.venue_order_id, ack.status), ("venue-9", "accepted"))

    def test_crypto_orders_are_gtc_and_limits_carry_their_price(self) -> None:
        venue = self._venue()
        venue.submit(VenueOrder("at-8", "BTC/USD", "buy", type="limit", qty=Decimal("0.001"),
                                limit_price=Decimal("50000")))
        request = self.client.submitted[0]
        self.assertEqual(str(getattr(request.time_in_force, "value", request.time_in_force)), "gtc")
        self.assertEqual((request.qty, request.limit_price), (0.001, 50000.0))

    def test_cancel_finds_the_order_by_our_id(self) -> None:
        venue = self._venue()
        venue.cancel("at-1")
        self.assertEqual(self.client.cancelled, ["venue-1"])
        self.assertEqual(venue.open_orders()[0].client_order_id, "at-1")
        self.assertEqual(venue.get("at-1").status, "new")
