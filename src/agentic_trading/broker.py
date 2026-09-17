"""Broker facade over Robinhood Trading MCP tool calls.

Contract notes (verified against the live tool schema, 2026-09-16):

- accounts come from ``get_accounts``; exactly one account is
  ``agentic_allowed=true``
- account value comes from ``get_portfolio(account_number)``
- positions come from ``get_equity_positions(account_number)``
- quotes come from ``get_equity_quotes(symbols)``
- orders go through ``review_equity_order`` / ``place_equity_order`` and
  require ``account_number`` plus exactly one of ``quantity``/``dollar_amount``

Parsing is **fail-closed**: unrecognised payload shapes raise
``BrokerPayloadError`` instead of silently reporting zero equity or flat
positions, because both drive real risk decisions. Run
``agentic-trading probe`` against an authenticated account to dump raw payloads
if a shape needs confirming.
"""

from __future__ import annotations

from decimal import Decimal, InvalidOperation
from typing import Any, Optional, Protocol

from agentic_trading.orders import EquityOrderRequest, is_crypto_symbol
from agentic_trading.rh_mcp.snapshot import build_capability_map
from agentic_trading.risk import PortfolioSnapshot

# Ordered most- to least-authoritative. Only keys that represent total account
# value are accepted; a market value for one asset class must never size risk.
_EQUITY_KEYS = (
    "total_equity",
    "equity",
    "portfolio_value",
    "account_value",
    "total_value",
    "total_account_value",
)

_LIST_KEYS = (
    "results",
    "positions",
    "orders",
    "accounts",
    "items",
    "equity_positions",
    "data",
)

_SYMBOL_KEYS = ("symbol", "instrument_symbol", "ticker")
_QUANTITY_KEYS = ("quantity", "qty", "shares", "quantity_available")


class BrokerPayloadError(RuntimeError):
    """A broker payload did not match any known shape (fail closed)."""


class McpClient(Protocol):
    def call_tool(self, name: str, arguments: dict[str, Any]) -> dict[str, Any]: ...

    def list_tools(self) -> list[dict[str, Any]]: ...


class Broker:
    def __init__(
        self,
        client: McpClient,
        tools: list[dict[str, Any]],
        capability_map: dict[str, str] | None = None,
        *,
        account_number: str | None = None,
    ) -> None:
        self._client = client
        self._tools = tools
        self._capability_map = capability_map or build_capability_map(tools)
        self._account_number = account_number or None
        self.last_equity_source: str = ""

    # -- accounts / value -------------------------------------------------

    def list_accounts(self) -> list[dict[str, Any]]:
        return _first_list(self._call("list_accounts", {}))

    def resolve_account_number(self) -> str:
        """Return the single ``agentic_allowed`` account number (cached)."""
        if self._account_number:
            return self._account_number

        allowed = [
            str(account.get("account_number"))
            for account in self.list_accounts()
            if account.get("agentic_allowed") is True and account.get("account_number")
        ]
        if not allowed:
            raise BrokerPayloadError(
                "no agentic_allowed account found; enable agentic trading for the "
                "account or set account_number in config"
            )
        if len(allowed) > 1:
            raise BrokerPayloadError(
                f"multiple agentic_allowed accounts: {allowed}; "
                "set account_number in config"
            )
        self._account_number = allowed[0]
        return self._account_number

    def resolve_rhs_account_number(self) -> str:
        """Numeric account id the crypto tools require.

        ``get_accounts`` returns both an alphanumeric ``account_number`` (used by
        every equity tool) and a numeric ``rhs_account_number`` (used by every
        crypto tool). They are not interchangeable.
        """
        for account in self.list_accounts():
            if (
                account.get("agentic_allowed") is True
                and account.get("rhs_account_number")
            ):
                return str(account["rhs_account_number"])
        raise BrokerPayloadError(
            "no agentic_allowed account exposes rhs_account_number; "
            "crypto tools cannot be called safely"
        )

    def get_portfolio(self, account_number: str | None = None) -> dict[str, Any]:
        account = account_number or self.resolve_account_number()
        return self._call("get_portfolio", {"account_number": account})

    def get_equity(self, account_number: str | None = None) -> Decimal:
        payload = self.get_portfolio(account_number)
        value, source = _extract_equity(payload)
        self.last_equity_source = source
        return value

    # -- positions --------------------------------------------------------

    def get_positions(self, account_number: str | None = None) -> PortfolioSnapshot:
        account = account_number or self.resolve_account_number()
        payload = self._call("get_positions", {"account_number": account})
        return _extract_positions(payload)

    # -- market data ------------------------------------------------------

    def get_quotes(self, symbols: list[str]) -> dict[str, Any]:
        if not symbols:
            raise ValueError("symbols required")
        if len(symbols) > 20:
            raise ValueError("get_equity_quotes accepts at most 20 symbols")
        return self._call(
            "get_quotes", {"symbols": [s.strip().upper() for s in symbols]}
        )

    def get_price_book(self, symbols: list[str]) -> dict[str, Any]:
        if not symbols or len(symbols) > 4:
            raise ValueError("get_equity_price_book accepts 1..4 symbols")
        return self._call(
            "get_price_book", {"symbols": [s.strip().upper() for s in symbols]}
        )

    def get_crypto_quotes(self, symbols: list[str]) -> dict[str, Any]:
        """24/7 crypto quotes (read-only).

        Robinhood's crypto namespace is separate from equity: it wants pair
        symbols such as ``BTC-USD`` and returns them undashed (``BTCUSD``).
        """
        if not symbols:
            raise ValueError("symbols required")
        return self._call(
            "get_crypto_quotes", {"symbols": [s.strip().upper() for s in symbols]}
        )

    def get_crypto_positions(
        self, *, rhs_account_number: str | None = None
    ) -> dict[str, Any]:
        """Crypto holdings, keyed by the numeric ``rhs_account_number``."""
        account = rhs_account_number or self.resolve_rhs_account_number()
        return self._call("get_crypto_positions", {"rhs_account_number": account})

    def get_crypto_position_snapshot(self) -> PortfolioSnapshot:
        """Crypto holdings as a pair-keyed :class:`PortfolioSnapshot`.

        The equity snapshot cannot see crypto, so without this a live crypto
        position is invisible: entries would stack past ``max_open_positions``
        and exits would be refused as ``would_short``, stranding the position.
        """
        return _extract_crypto_positions(self.get_crypto_positions())

    def get_crypto_orders(self) -> list[dict[str, Any]]:
        """Open and historical crypto orders (paginated, first page)."""
        account = self.resolve_rhs_account_number()
        return _first_list(
            self._call("get_crypto_orders", {"rhs_account_number": account})
        )

    def get_trade_history(self, *, span: str = "month") -> list[dict[str, Any]]:
        """Executed trades with their realized gain — the broker's own record."""
        account = self.resolve_rhs_account_number()
        payload = self._call(
            "get_trade_history", {"account_number": account, "span": span}
        )
        # This tool nests under data.trades rather than any of the shared list
        # keys, so it gets its own (still fail-closed) extraction.
        data = payload.get("data") if isinstance(payload, dict) else None
        rows = data.get("trades") if isinstance(data, dict) else None
        if rows is None and isinstance(data, dict) and "trades" in data:
            return []
        if not isinstance(rows, list):
            raise BrokerPayloadError(
                f"expected data.trades in trade history, got keys {_shape(payload)}"
            )
        return [row for row in rows if isinstance(row, dict)]

    def get_tradability(self, symbols: list[str]) -> dict[str, Any]:
        account = self.resolve_account_number()
        return self._call(
            "get_tradability",
            {"account_number": account, "symbols": [s.upper() for s in symbols]},
        )

    def get_historicals(
        self,
        symbols: list[str],
        *,
        start_time: str,
        end_time: str | None = None,
        interval: str | None = None,
        bounds: str = "regular",
        adjustment_type: str = "split",
    ) -> dict[str, Any]:
        """OHLCV bars for backtesting. Read-only; no account required."""
        if not symbols:
            raise ValueError("symbols required")
        if len(symbols) > 10:
            raise ValueError("get_equity_historicals accepts at most 10 symbols")
        args: dict[str, Any] = {
            "symbols": [s.strip().upper() for s in symbols],
            "start_time": start_time,
            "bounds": bounds,
            "adjustment_type": adjustment_type,
        }
        if end_time:
            args["end_time"] = end_time
        if interval:
            args["interval"] = interval
        return self._call("get_historicals", args)

    # -- orders -----------------------------------------------------------

    def review_order(self, request: EquityOrderRequest) -> dict[str, Any]:
        """Pre-trade review (read-only) in the namespace the symbol belongs to.

        Crypto orders must go to ``preview_crypto_order``: the equity review
        tool rejects the crypto argument shape with ``unexpected additional
        properties ["rhs_account_number"]``. Ref_id is a placement-only key and
        is dropped for both namespaces.
        """
        capability = (
            "preview_crypto" if is_crypto_symbol(request.symbol) else "review_equity"
        )
        return self._call(capability, request.to_mcp_args(include_ref_id=False))

    def place_order(self, request: EquityOrderRequest) -> dict[str, Any]:
        capability = (
            "place_crypto" if is_crypto_symbol(request.symbol) else "place_equity"
        )
        return self._call(capability, request.to_mcp_args())

    def get_orders(
        self,
        *,
        account_number: str | None = None,
        order_id: str | None = None,
        state_group: str | None = None,
        symbol: str | None = None,
    ) -> list[dict[str, Any]]:
        account = account_number or self.resolve_account_number()
        args: dict[str, Any] = {"account_number": account}
        if order_id:
            args["order_id"] = order_id
        if state_group:
            args["state_group"] = state_group
        if symbol:
            args["symbol"] = symbol.upper()
        return _first_list(self._call("get_orders", args))

    def cancel_order(
        self, order_id: str, *, account_number: str | None = None
    ) -> dict[str, Any]:
        account = account_number or self.resolve_account_number()
        return self._call(
            "cancel_equity", {"account_number": account, "order_id": order_id}
        )

    # -- internals --------------------------------------------------------

    def _call(self, capability: str, arguments: dict[str, Any]) -> dict[str, Any]:
        tool = self._capability_map.get(capability)
        if not tool:
            raise KeyError(
                f"no MCP tool mapped for capability {capability!r}; "
                f"snapshot has {sorted(self._capability_map)}"
            )
        return self._client.call_tool(tool, dict(arguments))


def _first_list(payload: Any, *, _depth: int = 0) -> list[dict[str, Any]]:
    """Pull the first list of objects out of a payload, or raise.

    Live payloads nest one level deep (e.g. ``{"data": {"accounts": [...]}}``),
    so known container keys are traversed before giving up.
    """
    if isinstance(payload, list):
        return [item for item in payload if isinstance(item, dict)]
    if isinstance(payload, dict):
        for key in _LIST_KEYS:
            value = payload.get(key)
            if isinstance(value, list):
                return [item for item in value if isinstance(item, dict)]
            if value is None and key in payload and key != "data":
                # Live schemas type list outputs as ["null","array"]: an empty
                # page serializes as null, which is "no rows", not a failure.
                return []
        if _depth < 3:
            for key in _LIST_KEYS:
                value = payload.get(key)
                if isinstance(value, dict):
                    try:
                        return _first_list(value, _depth=_depth + 1)
                    except BrokerPayloadError:
                        continue
    raise BrokerPayloadError(f"expected a list of objects, got keys {_shape(payload)}")


def _extract_equity(payload: Any, *, _depth: int = 0) -> tuple[Decimal, str]:
    """Find total account value in a portfolio payload."""
    if _depth > 3:
        raise BrokerPayloadError("equity not found in nested portfolio payload")
    if isinstance(payload, dict):
        for key in _EQUITY_KEYS:
            if key in payload:
                value = _to_decimal(payload[key])
                if value is not None and value > 0:
                    return value, key
        for key in ("portfolio", "account", "data", "results", "value"):
            if key in payload:
                try:
                    return _extract_equity(payload[key], _depth=_depth + 1)
                except BrokerPayloadError:
                    continue
    elif isinstance(payload, list):
        for item in payload:
            try:
                return _extract_equity(item, _depth=_depth + 1)
            except BrokerPayloadError:
                continue
    raise BrokerPayloadError(
        "could not determine account equity from portfolio payload "
        f"(keys: {_shape(payload)}); run 'agentic-trading probe' to inspect it"
    )


def _extract_crypto_positions(payload: Any) -> PortfolioSnapshot:
    """Parse crypto holdings into a pair-keyed snapshot.

    Recorded live shape (``get_crypto_positions`` outputSchema):
    ``{"data": {"results": [{"currency": {"code": "BTC"}, "quantity": "0.001"}]}}``.
    A ``null`` or empty page means "no crypto held"; anything unrecognized is a
    read failure, because this snapshot is what stops a live crypto entry from
    stacking and an exit from being refused as an oversell (fail closed).
    """
    try:
        rows = _first_list(payload)
    except BrokerPayloadError:
        return PortfolioSnapshot(open_positions=0, held={}, positions_read_failed=True)

    held: dict[str, Decimal] = {}
    for row in rows:
        currency = row.get("currency")
        code = ""
        if isinstance(currency, dict):
            code = str(currency.get("code") or "")
        if not code:
            code = str(row.get("currency_code") or "")
        quantity = _to_decimal(row.get("quantity"))
        if not code or quantity is None or quantity == 0:
            continue
        held[f"{code.strip().upper()}-USD"] = quantity
    return PortfolioSnapshot(open_positions=len(held), held=held)


def _extract_positions(payload: Any, *, _depth: int = 0) -> PortfolioSnapshot:
    """Parse an open-positions payload. No list found ⇒ read failure (fail closed)."""
    items: Any = None
    if isinstance(payload, list):
        items = payload
    elif isinstance(payload, dict):
        for key in _LIST_KEYS:
            if isinstance(payload.get(key), list):
                items = payload[key]
                break
            if payload.get(key) is None and key in payload and key != "data":
                items = []
                break
        if items is None and _depth < 3:
            for key in _LIST_KEYS:
                value = payload.get(key)
                if isinstance(value, dict):
                    nested = _extract_positions(value, _depth=_depth + 1)
                    # Live payloads wrap data: {"data": {"positions": []}}.
                    if not nested.positions_read_failed or nested.held:
                        return nested
    if items is None:
        return PortfolioSnapshot(open_positions=0, held={}, positions_read_failed=True)

    held: dict[str, Decimal] = {}
    for item in items:
        if not isinstance(item, dict):
            continue
        symbol = ""
        for key in _SYMBOL_KEYS:
            if item.get(key):
                symbol = str(item[key]).upper()
                break
        if not symbol:
            continue
        quantity: Optional[Decimal] = None
        for key in _QUANTITY_KEYS:
            if item.get(key) is not None:
                quantity = _to_decimal(item[key])
                if quantity is not None:
                    break
        if quantity is None or quantity <= 0:
            continue
        held[symbol] = held.get(symbol, Decimal("0")) + quantity
    return PortfolioSnapshot(open_positions=len(held), held=held)


def _to_decimal(value: Any) -> Optional[Decimal]:
    if isinstance(value, bool) or value is None:
        return None
    try:
        result = Decimal(str(value))
    except (InvalidOperation, ValueError):
        return None
    return result if result.is_finite() else None


def _shape(payload: Any, *, limit: int = 12) -> str:
    if isinstance(payload, dict):
        return f"{list(payload)[:limit]}"
    if isinstance(payload, list):
        return f"list[{len(payload)}]"
    return type(payload).__name__
