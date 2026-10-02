"""Coinbase Advanced Trade: the crypto ticker stream and a live account.

Coinbase has no paper mode, so this venue is live-only and every order passes
the guard's arming check. Messages are parsed from the raw JSON rather than
through SDK response classes, so a field the SDK has not modelled yet cannot
break the stream.
"""

from __future__ import annotations

import json
import re
import threading
import time
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from typing import Any, Callable, Optional

from agentic_trading.venues.model import AccountView, PositionView, Tick, VenueAck, VenueOrder
from agentic_trading.venues.secrets import Credentials

Emit = Callable[[Tick], None]
DATA_SOURCE = "coinbase"
CASH = ("USD", "USDC")
_FRACTION = re.compile(r"\.(\d{6})\d+")


class VenueError(RuntimeError):
    """The venue refused or failed an order."""


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


def _time(raw: Any) -> Optional[datetime]:
    if not isinstance(raw, str):
        return None
    text = _FRACTION.sub(r".\1", raw.replace("Z", "+00:00"))  # nanoseconds -> micro
    try:
        stamp = datetime.fromisoformat(text)
    except ValueError:
        return None
    return stamp if stamp.tzinfo else stamp.replace(tzinfo=timezone.utc)


def ticker_to_ticks(message: Any, received_at: datetime) -> list[Tick]:
    try:
        data = json.loads(message) if isinstance(message, (str, bytes)) else message
    except ValueError:
        return []
    if not isinstance(data, dict) or data.get("channel") != "ticker":
        return []
    at = _time(data.get("timestamp"))
    events = data.get("events")
    if at is None or not isinstance(events, list):
        return []
    ticks: list[Tick] = []
    for event in events:
        tickers = event.get("tickers") if isinstance(event, dict) else None
        for item in tickers if isinstance(tickers, list) else []:
            if not isinstance(item, dict) or not item.get("product_id"):
                continue
            bid, ask = _positive(item.get("best_bid")), _positive(item.get("best_ask"))
            last = _positive(item.get("price"))
            if bid is None and ask is None and last is None:
                continue
            ticks.append(Tick(DATA_SOURCE, str(item["product_id"]), bid, ask, last, None, at, received_at))
    return ticks


def _default_ws(credentials: Credentials, on_message: Callable[[str], None]) -> Any:  # pragma: no cover - SDK
    from coinbase.websocket import WSClient

    return WSClient(api_key=credentials.key, api_secret=credentials.secret, on_message=on_message)


class CoinbaseTicks:
    key = "coinbase"
    venue = DATA_SOURCE
    always_open = True

    def __init__(
        self,
        credentials: Credentials,
        products: Any,
        *,
        client_factory: Optional[Callable[[Credentials, Callable[[str], None]], Any]] = None,
        clock: Optional[Callable[[], datetime]] = None,
        poll_every: float = 1.0,
        silent_after: float = 30.0,
    ) -> None:
        self.credentials = credentials
        self.symbols = tuple(products)
        self._factory = client_factory or _default_ws
        self._clock = clock or _now
        self.poll_every, self.silent_after = poll_every, silent_after
        self._stopped = threading.Event()

    def run(self, emit: Emit) -> None:
        """Stream until ``stop()``, a background error, or silence.

        The SDK's ``run_forever_with_exception_check`` never returns after a
        close, ours or the server's, so this loop owns the lifetime instead.
        The heartbeats channel speaks every second, so a quiet socket is a
        dead one: raising lets the supervisor reconnect it.
        """
        if self._stopped.is_set():
            return
        heard = [time.monotonic()]

        def on_message(message: str) -> None:
            heard[0] = time.monotonic()
            try:
                for tick in ticker_to_ticks(message, self._clock()):
                    emit(tick)
            except Exception:  # noqa: BLE001 - one bad message never ends the stream
                pass

        client = self._factory(self.credentials, on_message)
        try:
            client.open()
            client.subscribe(product_ids=list(self.symbols), channels=["ticker", "heartbeats"])
            while not self._stopped.wait(self.poll_every):
                client.raise_background_exception()
                if time.monotonic() - heard[0] > self.silent_after:
                    raise ConnectionError(f"no message from coinbase for {self.silent_after:g}s")
        finally:
            try:
                client.close()
            except Exception:  # noqa: BLE001 - already closed, or never opened
                pass

    def stop(self) -> None:
        self._stopped.set()  # run() notices within poll_every and closes the socket


def _as_dict(response: Any) -> dict[str, Any]:
    if isinstance(response, dict):
        return response
    if hasattr(response, "to_dict"):
        return response.to_dict()
    return json.loads(json.dumps(response, default=lambda o: getattr(o, "__dict__", str(o))))


def _default_rest(credentials: Credentials) -> Any:  # pragma: no cover - SDK
    from coinbase.rest import RESTClient

    return RESTClient(api_key=credentials.key, api_secret=credentials.secret)


class CoinbaseVenue:
    name = "coinbase"
    mode = "live"

    def __init__(
        self,
        credentials: Credentials,
        *,
        client_factory: Optional[Callable[[Credentials], Any]] = None,
    ) -> None:
        self._client = (client_factory or _default_rest)(credentials)
        self._venue_ids: dict[str, str] = {}

    def _balances(self) -> list[tuple[str, Decimal]]:
        accounts = _as_dict(self._client.get_accounts()).get("accounts") or []
        out = []
        for account in accounts:
            balance = (account.get("available_balance") or {}).get("value")
            amount = _dec(balance)
            if account.get("currency") and amount is not None:
                out.append((str(account["currency"]), amount))
        return out

    def account(self) -> AccountView:
        cash = sum((amount for currency, amount in self._balances() if currency in CASH), Decimal("0"))
        # Holdings are listed by positions(); equity here counts cash only
        # because pricing them needs the ticker, which the venue does not own.
        return AccountView(self.name, self.mode, cash, cash, cash)

    def positions(self) -> list[PositionView]:
        return [
            PositionView(f"{currency}-USD", amount, Decimal("0"))
            for currency, amount in self._balances()
            if currency not in CASH and amount > 0
        ]

    def open_orders(self) -> list[VenueAck]:
        orders = _as_dict(self._client.list_orders(order_status=["OPEN"])).get("orders") or []
        return [
            VenueAck(
                client_order_id=str(o.get("client_order_id", "")),
                venue_order_id=str(o.get("order_id", "")),
                status=str(o.get("status", "")),
                filled_qty=_dec(o.get("filled_size")) or Decimal("0"),
                filled_avg_price=_dec(o.get("average_filled_price")),
            )
            for o in orders
        ]

    def submit(self, order: VenueOrder) -> VenueAck:
        ids = {"client_order_id": order.client_order_id, "product_id": order.symbol}
        if order.type == "market" and order.side == "buy":
            if order.notional is None:
                raise ValueError("Coinbase market buys are sized in dollars (notional)")
            response = self._client.market_order_buy(**ids, quote_size=str(order.notional))
        elif order.type == "market":
            if order.qty is None:
                raise ValueError("Coinbase market sells are sized in coins (qty)")
            response = self._client.market_order_sell(**ids, base_size=str(order.qty))
        else:
            if order.qty is None:
                raise ValueError("Coinbase limit orders are sized in coins (qty)")
            method = self._client.limit_order_gtc_buy if order.side == "buy" else self._client.limit_order_gtc_sell
            response = method(**ids, base_size=str(order.qty), limit_price=str(order.limit_price))
        data = _as_dict(response)
        if not data.get("success"):
            reason = (data.get("error_response") or {}).get("message") or "refused"
            raise VenueError(f"Coinbase refused the order: {reason}")
        venue_id = str((data.get("success_response") or {}).get("order_id", ""))
        self._venue_ids[order.client_order_id] = venue_id
        return VenueAck(order.client_order_id, venue_id, "submitted")

    def cancel(self, client_order_id: str) -> None:
        venue_id = self._venue_ids[client_order_id]  # KeyError: never sent from here
        self._client.cancel_orders(order_ids=[venue_id])
