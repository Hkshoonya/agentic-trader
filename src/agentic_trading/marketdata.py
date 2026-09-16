"""Quote feeds for the autonomous runtime.

Two sources, same normalized quote dict contract as the JSONL files:

- :class:`FileQuoteFeed` — tails a collector-written JSONL file (append-only)
- :class:`McpQuoteFeed` — polls ``get_equity_quotes`` through the broker

Normalization is fail-closed: if a quote lacks a usable two-sided market or the
payload shape is unrecognised, no quote is produced. Strategies never invent a
price, so a malformed feed results in no trades rather than bad trades.
"""

from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any, Callable, Optional, Protocol

from agentic_trading.broker import Broker
from agentic_trading.config import Config
from agentic_trading.orders import is_crypto_symbol
from agentic_trading.quotes import _normalize_quote

_ENTRY_LIST_KEYS = ("quotes", "results", "items", "data", "equity_quotes")
_SYMBOL_KEYS = ("symbol", "ticker", "instrument_symbol", "symbols")
_BID_KEYS = ("bid_price", "bid", "bidPrice")
_ASK_KEYS = ("ask_price", "ask", "askPrice")
_TIME_KEYS = (
    "quote_time",
    "quote_at",
    "updated_at",
    "timestamp",
    "time",
    "venue_bid_time",
    "venue_ask_time",
    "venue_last_trade_time",
)


class QuoteShapeError(RuntimeError):
    """The quote payload did not match any known shape (fail closed)."""


class QuoteFeed(Protocol):
    def poll(self) -> list[dict]: ...


# --------------------------------------------------------------------------
# normalisation
# --------------------------------------------------------------------------


def normalize_quotes_payload(
    payload: Any,
    *,
    symbols: list[str],
    observed_at: datetime,
) -> list[dict]:
    """Convert a ``get_equity_quotes`` payload into normalized quote dicts.

    Quotes without both bid and ask are dropped (never synthesized).
    """
    entries = _extract_entries(payload)
    if entries is None:
        raise QuoteShapeError(
            "unrecognised quotes payload; run 'agentic-trading probe' to inspect it"
        )

    wanted = {s.upper() for s in symbols}
    quotes: list[dict] = []
    for fallback_symbol, entry in entries:
        symbol = _symbol_of(entry, fallback_symbol)
        if not symbol or (wanted and symbol not in wanted):
            continue
        bid = _decimal_of(entry, _BID_KEYS)
        ask = _decimal_of(entry, _ASK_KEYS)
        if bid is None or ask is None or bid <= 0 or ask < bid:
            continue
        quoted_at = _time_of(entry, default=observed_at)
        quotes.append(
            {
                "symbol": symbol,
                "observed_at": observed_at.isoformat(),
                "quote_at": quoted_at.isoformat(),
                "bid": bid,
                "ask": ask,
                "source": "Robinhood MCP",
                "session": "mcp",
                "delayed": None,
            }
        )
    return quotes


def _extract_entries(
    payload: Any, *, _depth: int = 0
) -> Optional[list[tuple[str, dict]]]:
    """Return ``[(fallback_symbol, entry), ...]`` or ``None`` if unrecognised."""
    if isinstance(payload, list):
        return [
            ("", _unwrap_quote(item)) for item in payload if isinstance(item, dict)
        ]

    if not isinstance(payload, dict):
        return None

    for key in _ENTRY_LIST_KEYS:
        value = payload.get(key)
        if isinstance(value, list):
            return [("", _unwrap_quote(item)) for item in value if isinstance(item, dict)]
        if isinstance(value, dict):
            # Symbol-keyed mapping: {"SPY": {...}, "QQQ": {...}}
            mapped: list[tuple[str, dict]] = []
            for symbol, item in value.items():
                if isinstance(item, dict):
                    mapped.append((str(symbol).upper(), _unwrap_quote(item)))
            if mapped:
                return mapped
            if _depth < 3:
                nested = _extract_entries(value, _depth=_depth + 1)
                if nested:
                    return nested
    if _depth < 3:
        # Live payloads nest the list one level down: {"data": {"results": [...]}}
        for key, value in payload.items():
            if isinstance(value, dict):
                nested = _extract_entries(value, _depth=_depth + 1)
                if nested:
                    return nested

    # A single quote object for one symbol.
    if any(k in payload for k in _BID_KEYS) and any(k in payload for k in _ASK_KEYS):
        return [("", payload)]

    return None


def _unwrap_quote(entry: dict) -> dict:
    """Live quotes arrive as ``{"quote": {...}, "close": {...}}``."""
    nested = entry.get("quote")
    if isinstance(nested, dict):
        merged = dict(nested)
        for key, value in entry.items():
            if key != "quote":
                merged.setdefault(key, value)
        return merged
    return entry


def _symbol_of(entry: dict, fallback: str) -> str:
    for key in _SYMBOL_KEYS:
        value = entry.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip().upper()
        if isinstance(value, list) and value and isinstance(value[0], str):
            return value[0].strip().upper()
    return fallback.upper()


def _decimal_of(entry: dict, keys: tuple[str, ...]) -> Optional[Decimal]:
    for key in keys:
        if key not in entry:
            continue
        value = entry[key]
        if isinstance(value, dict):  # e.g. {"amount": "123.45"}
            value = value.get("amount") or value.get("price")
        if value is None or isinstance(value, bool):
            continue
        try:
            result = Decimal(str(value))
        except (InvalidOperation, ValueError):
            continue
        if result.is_finite() and result > 0:
            return result
    return None


def _time_of(entry: dict, *, default: datetime) -> datetime:
    for key in _TIME_KEYS:
        if key not in entry:
            continue
        raw = entry[key]
        parsed = _parse_time(raw)
        if parsed is not None:
            return parsed
    return default


def _parse_time(raw: Any) -> Optional[datetime]:
    if raw is None:
        return None
    if isinstance(raw, (int, float)) and not isinstance(raw, bool):
        try:
            return datetime.fromtimestamp(float(raw), tz=timezone.utc)
        except (OverflowError, OSError, ValueError):
            return None
    if isinstance(raw, str):
        text = raw.strip()
        if not text:
            return None
        try:
            parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
        except ValueError:
            try:
                return datetime.fromtimestamp(float(text), tz=timezone.utc)
            except (OverflowError, OSError, ValueError):
                return None
        if parsed.tzinfo is None:
            return parsed.replace(tzinfo=timezone.utc)
        return parsed.astimezone(timezone.utc)
    return None


# --------------------------------------------------------------------------
# feeds
# --------------------------------------------------------------------------


class FileQuoteFeed:
    """Tail a JSONL quote file, yielding only complete lines appended since last poll.

    On the first poll this returns the file's existing complete lines; the
    runtime's freshness guard is what stops a replayed recording from driving a
    live order, so there is exactly one staleness mechanism to reason about.
    """

    def __init__(self, path: Path | str) -> None:
        self.path = Path(path)
        self._offset = 0

    def poll(self) -> list[dict]:
        import json

        if not self.path.is_file():
            return []
        quotes: list[dict] = []
        with self.path.open(encoding="utf-8") as handle:
            handle.seek(self._offset)
            for line in handle:
                if not line.endswith("\n"):
                    # Tolerate a partially written final line: retry it next poll.
                    break
                self._offset += len(line.encode("utf-8"))
                stripped = line.strip()
                if not stripped:
                    continue
                try:
                    raw = json.loads(stripped)
                except json.JSONDecodeError:
                    continue
                quote = _normalize_quote(raw)
                if quote is not None:
                    quotes.append(quote)
        return quotes


class McpQuoteFeed:
    """Poll real-time quotes from the Robinhood MCP broker."""

    def __init__(
        self,
        broker: Broker,
        symbols: list[str],
        *,
        clock: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
    ) -> None:
        if not symbols:
            raise ValueError("at least one symbol required")
        self.broker = broker
        self.symbols = [s.strip().upper() for s in symbols]
        self.clock = clock

    def poll(self) -> list[dict]:
        payload = self.broker.get_quotes(self.symbols)
        return normalize_quotes_payload(
            payload, symbols=self.symbols, observed_at=self.clock()
        )


class CryptoQuoteFeed(McpQuoteFeed):
    """Polls ``get_crypto_quotes`` — the only market open around the clock."""

    def poll(self) -> list[dict]:
        payload = self.broker.get_crypto_quotes(self.symbols)
        # The crypto namespace returns pairs without the dash (BTCUSD), while
        # callers supply the broker form (BTC-USD). Compare on the undashed
        # form or every quote is filtered out and the feed looks empty.
        wanted = [symbol.replace("-", "").upper() for symbol in self.symbols]
        return normalize_quotes_payload(
            payload, symbols=wanted, observed_at=self.clock()
        )


class CompositeQuoteFeed:
    """Polls several feeds and concatenates their quotes."""

    def __init__(self, feeds: list[QuoteFeed]) -> None:
        self.feeds = list(feeds)

    def poll(self) -> list[dict]:
        quotes: list[dict] = []
        for feed in self.feeds:
            quotes.extend(feed.poll())
        return quotes


def is_crypto_pair(symbol: str) -> bool:
    """Whitelist convention: ``BTC-USD`` (or ``BTCUSD``) is crypto, ``SPY`` is not."""
    # Single source of truth: orders owns this because broker/orders cannot
    # import marketdata (marketdata imports broker).
    return is_crypto_symbol(symbol)


def build_quote_feed(config: Config, broker: Broker) -> QuoteFeed:
    if config.quote_source == "mcp":
        equity = sorted(s for s in config.symbol_whitelist if not is_crypto_pair(s))
        crypto = sorted(s for s in config.symbol_whitelist if is_crypto_pair(s))
        feeds: list[QuoteFeed] = []
        if equity:
            feeds.append(McpQuoteFeed(broker, equity))
        if crypto:
            feeds.append(CryptoQuoteFeed(broker, crypto))
        if not feeds:
            return McpQuoteFeed(broker, sorted(config.symbol_whitelist))
        return feeds[0] if len(feeds) == 1 else CompositeQuoteFeed(feeds)
    if config.quote_source == "file":
        return FileQuoteFeed(config.quotes_path)
    raise ValueError(f"unknown quote_source: {config.quote_source!r}")
