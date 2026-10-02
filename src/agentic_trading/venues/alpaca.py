"""Alpaca: paper and live accounts, stock and crypto streams, via alpaca-py.

Each stream's blocking ``run()`` is called in a thread owned by the
supervisor (``daemon.supervise``). The SDK's handlers are async functions run
on the SDK's own loop, so they only convert and hand the tick to ``emit``.
Any error in a single message is swallowed there: a bad message must never
end the stream.
"""

from __future__ import annotations

import logging
import threading
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from typing import Any, Callable, Optional

from agentic_trading.venues.model import AccountView, PositionView, Tick, VenueAck, VenueOrder
from agentic_trading.venues.secrets import Credentials

Emit = Callable[[Tick], None]
DATA_SOURCE = "alpaca"


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _dec(value: Any) -> Optional[Decimal]:
    if value is None:
        return None
    try:
        number = Decimal(str(value))
    except (InvalidOperation, ValueError):
        return None
    return number if number.is_finite() else None


def _positive(value: Any) -> Optional[Decimal]:
    number = _dec(value)
    return number if number is not None and number > 0 else None


def _utc(value: Any) -> Optional[datetime]:
    if not isinstance(value, datetime):
        return None
    return value if value.tzinfo else value.replace(tzinfo=timezone.utc)


def quote_to_tick(quote: Any, venue: str, received_at: datetime) -> Optional[Tick]:
    symbol = getattr(quote, "symbol", None)
    at = _utc(getattr(quote, "timestamp", None))
    bid = _positive(getattr(quote, "bid_price", None))
    ask = _positive(getattr(quote, "ask_price", None))
    if not symbol or at is None or (bid is None and ask is None):
        return None
    return Tick(venue, str(symbol), bid, ask, None, None, at, received_at)


def trade_to_tick(trade: Any, venue: str, received_at: datetime) -> Optional[Tick]:
    symbol = getattr(trade, "symbol", None)
    at = _utc(getattr(trade, "timestamp", None))
    price = _positive(getattr(trade, "price", None))
    if not symbol or at is None or price is None:
        return None
    return Tick(venue, str(symbol), None, None, price, _dec(getattr(trade, "size", None)), at, received_at)


class _LoginRefused(logging.Filter):
    """Spots a refused stream login, which the SDK only logs, then retries forever.

    The record is dropped (one traceback per retry would fill the service log)
    and the stream is stopped from a helper thread, so ``run`` returns and the
    supervisor can mark the stream ``auth_failed`` instead of retrying.
    """

    WORDS = ("auth failed", "failed to authenticate")

    def __init__(self, stream: Any) -> None:
        super().__init__()
        self.stream = stream
        self.thread = threading.get_ident()  # the SDK logs from the thread running it
        self.refused = False

    def filter(self, record: logging.LogRecord) -> bool:
        if record.thread != self.thread:
            return True
        if not any(word in record.getMessage().lower() for word in self.WORDS):
            return True
        if not self.refused:
            self.refused = True
            threading.Thread(target=self._stop, daemon=True).start()
        return False

    def _stop(self) -> None:
        try:
            self.stream.stop()
        except Exception:  # noqa: BLE001 - run() ends on its own when the loop does
            pass


class _AlpacaTicks:
    key = ""
    always_open = False
    venue = DATA_SOURCE

    def __init__(
        self,
        credentials: Credentials,
        symbols: Any,
        *,
        stream_factory: Optional[Callable[[Credentials], Any]] = None,
        clock: Optional[Callable[[], datetime]] = None,
    ) -> None:
        self.credentials = credentials
        self.symbols = tuple(symbols)
        self._factory = stream_factory or self._default_factory
        self._clock = clock or _now
        self._stream: Any = None

    def _default_factory(self, credentials: Credentials) -> Any:  # pragma: no cover - SDK
        raise NotImplementedError

    def run(self, emit: Emit) -> None:
        stream = self._factory(self.credentials)
        self._stream = stream

        def deliver(tick: Optional[Tick]) -> None:
            if tick is None:
                return
            try:
                emit(tick)
            except Exception:  # noqa: BLE001 - a subscriber bug must not end the stream
                pass

        async def on_quote(quote: Any) -> None:
            try:
                deliver(quote_to_tick(quote, self.venue, self._clock()))
            except Exception:  # noqa: BLE001 - one bad message is dropped
                pass

        async def on_trade(trade: Any) -> None:
            try:
                deliver(trade_to_tick(trade, self.venue, self._clock()))
            except Exception:  # noqa: BLE001 - one bad message is dropped
                pass

        stream.subscribe_quotes(on_quote, *self.symbols)
        stream.subscribe_trades(on_trade, *self.symbols)
        refused = _LoginRefused(stream)
        sdk_log = logging.getLogger("alpaca.data.live.websocket")
        sdk_log.addFilter(refused)
        try:
            stream.run()
        finally:
            sdk_log.removeFilter(refused)
        if refused.refused:
            raise PermissionError("alpaca data stream login failed: auth failed")

    def stop(self) -> None:
        stream = self._stream
        if stream is not None:
            stream.stop()


class AlpacaStockTicks(_AlpacaTicks):
    key = "alpaca_stocks"
    always_open = False

    def __init__(self, credentials: Credentials, symbols: Any, *, feed: str = "iex", **kwargs: Any) -> None:
        super().__init__(credentials, symbols, **kwargs)
        self.feed = feed

    def _default_factory(self, credentials: Credentials) -> Any:  # pragma: no cover - SDK
        from alpaca.data.enums import DataFeed
        from alpaca.data.live import StockDataStream

        return StockDataStream(credentials.key, credentials.secret, feed=DataFeed(self.feed))


class AlpacaCryptoTicks(_AlpacaTicks):
    key = "alpaca_crypto"
    always_open = True

    def _default_factory(self, credentials: Credentials) -> Any:  # pragma: no cover - SDK
        from alpaca.data.live import CryptoDataStream

        return CryptoDataStream(credentials.key, credentials.secret)


def build_order_request(order: VenueOrder) -> Any:
    from alpaca.trading.enums import OrderSide, TimeInForce
    from alpaca.trading.requests import LimitOrderRequest, MarketOrderRequest

    fields: dict[str, Any] = {
        "symbol": order.symbol,
        "side": OrderSide.BUY if order.side == "buy" else OrderSide.SELL,
        # Crypto trades around the clock and accepts GTC/IOC, not DAY.
        "time_in_force": TimeInForce.GTC if "/" in order.symbol else TimeInForce.DAY,
        "client_order_id": order.client_order_id,
    }
    if order.notional is not None:
        fields["notional"] = float(order.notional)
    else:
        fields["qty"] = float(order.qty)  # type: ignore[arg-type]
    if order.type == "limit":
        return LimitOrderRequest(limit_price=float(order.limit_price), **fields)  # type: ignore[arg-type]
    return MarketOrderRequest(**fields)


def _ack(order: Any) -> VenueAck:
    status = getattr(order.status, "value", order.status)
    return VenueAck(
        client_order_id=str(order.client_order_id),
        venue_order_id=str(order.id),
        status=str(status),
        filled_qty=_dec(order.filled_qty) or Decimal("0"),
        filled_avg_price=_dec(order.filled_avg_price),
    )


def _default_trading_client(credentials: Credentials, paper: bool) -> Any:  # pragma: no cover - SDK
    from alpaca.trading.client import TradingClient

    return TradingClient(credentials.key, credentials.secret, paper=paper)


class AlpacaVenue:
    def __init__(
        self,
        credentials: Credentials,
        *,
        paper: bool,
        client_factory: Optional[Callable[[Credentials, bool], Any]] = None,
    ) -> None:
        self.name = "alpaca_paper" if paper else "alpaca_live"
        self.mode = "paper" if paper else "live"
        self._client = (client_factory or _default_trading_client)(credentials, paper)

    def account(self) -> AccountView:
        a = self._client.get_account()
        return AccountView(
            venue=self.name,
            mode=self.mode,
            equity=_dec(a.equity) or Decimal("0"),
            cash=_dec(a.cash) or Decimal("0"),
            buying_power=_dec(a.buying_power) or Decimal("0"),
            day_trades=int(getattr(a, "daytrade_count", 0) or 0),
            pattern_day_trader=bool(getattr(a, "pattern_day_trader", False)),
        )

    def positions(self) -> list[PositionView]:
        return [
            PositionView(str(p.symbol), _dec(p.qty) or Decimal("0"), _dec(p.market_value) or Decimal("0"))
            for p in self._client.get_all_positions()
        ]

    def open_orders(self) -> list[VenueAck]:
        try:
            from alpaca.trading.enums import QueryOrderStatus
            from alpaca.trading.requests import GetOrdersRequest

            request: Any = GetOrdersRequest(status=QueryOrderStatus.OPEN)
        except ImportError:  # pragma: no cover - the SDK is part of the venues extra
            request = None
        return [_ack(o) for o in self._client.get_orders(filter=request)]

    def submit(self, order: VenueOrder) -> VenueAck:
        return _ack(self._client.submit_order(order_data=build_order_request(order)))

    def get(self, client_order_id: str) -> VenueAck:
        return _ack(self._client.get_order_by_client_id(client_order_id))

    def cancel(self, client_order_id: str) -> None:
        found = self._client.get_order_by_client_id(client_order_id)
        self._client.cancel_order_by_id(found.id)
