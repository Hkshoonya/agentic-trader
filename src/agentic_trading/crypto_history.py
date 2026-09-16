"""Crypto OHLCV from public exchanges.

Robinhood's MCP exposes no crypto historicals, which makes rare-event crypto
strategies untestable through it. Public exchanges publish their full history,
so this adapter fills the one gap that mattered: real crypto bars, long enough
that a cascade-buying rule sees hundreds of events instead of dozens.

Kraken is primary (720 bars per request, deep history, no key). Binance is
geo-blocked from some networks and Coinbase caps at 300 bars per request, so
Kraken is both the cheapest and the most reliable here.
"""

from __future__ import annotations

import json
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any, Callable

from agentic_trading.history import Bar, save_bars

KRAKEN_OHLC = "https://api.kraken.com/0/public/OHLC"
MAX_BARS_PER_REQUEST = 720

# Kraken quotes the oldest coins against XBT rather than BTC.
KRAKEN_PAIRS = {
    "BTCUSD": "XBTUSD",
    "ETHUSD": "ETHUSD",
    "SOLUSD": "SOLUSD",
    "ADAUSD": "ADAUSD",
    "XDGUSD": "XDGUSD",
    "LTCUSD": "LTCUSD",
}


class CryptoHistoryError(RuntimeError):
    """Exchange returned an error or an unrecognised payload."""


def _http_json(url: str, *, timeout: float = 30.0) -> dict[str, Any]:
    request = urllib.request.Request(
        url, headers={"User-Agent": "agentic-trading/0.1 (research)"}
    )
    with urllib.request.urlopen(request, timeout=timeout) as response:
        payload = json.loads(response.read().decode("utf-8"))
    if not isinstance(payload, dict):
        raise CryptoHistoryError(f"unexpected payload type from {url}")
    return payload


def _parse_rows(rows: list[Any], symbol: str) -> list[Bar]:
    bars: list[Bar] = []
    for row in rows:
        if not isinstance(row, list) or len(row) < 5:
            continue
        try:
            start = datetime.fromtimestamp(float(row[0]), tz=timezone.utc)
            open_ = Decimal(str(row[1]))
            high = Decimal(str(row[2]))
            low = Decimal(str(row[3]))
            close = Decimal(str(row[4]))
        except (TypeError, ValueError):
            continue
        if min(open_, high, low, close) <= 0:
            continue
        volume = None
        if len(row) > 6:
            try:
                volume = Decimal(str(row[6]))
            except (TypeError, ValueError):
                volume = None
        bars.append(
            Bar(
                symbol=symbol,
                start=start,
                open=open_,
                high=high,
                low=low,
                close=close,
                volume=volume,
            )
        )
    bars.sort(key=lambda bar: bar.start)
    return bars


def fetch_kraken_daily(
    symbol: str,
    *,
    since: int | None = None,
    max_requests: int = 40,
    pause: float = 0.7,
    fetch: Callable[[str], dict[str, Any]] = _http_json,
) -> list[Bar]:
    """Page Kraken's daily OHLC until history is exhausted."""
    pair = KRAKEN_PAIRS.get(symbol.upper(), symbol.upper())
    collected: list[Bar] = []
    cursor = since
    seen_times: set[float] = set()

    for _ in range(max_requests):
        query = {"pair": pair, "interval": 1440}
        if cursor is not None:
            query["since"] = str(int(cursor))
        payload = fetch(f"{KRAKEN_OHLC}?{urllib.parse.urlencode(query)}")
        errors = payload.get("error")
        if errors:
            raise CryptoHistoryError(f"kraken error for {symbol}: {errors}")
        result = payload.get("result")
        if not isinstance(result, dict):
            raise CryptoHistoryError(f"kraken result missing for {symbol}")
        rows: list[Any] = []
        for key, value in result.items():
            if key == "last":
                continue
            if isinstance(value, list):
                rows = value
        if not rows:
            break
        fresh = [row for row in rows if float(row[0]) not in seen_times]
        if not fresh:
            break
        for row in fresh:
            seen_times.add(float(row[0]))
        collected.extend(_parse_rows(fresh, symbol.upper()))
        if len(rows) < MAX_BARS_PER_REQUEST:
            break
        last = result.get("last")
        if last is None or int(last) <= int(cursor or 0):
            break
        cursor = int(last)
        if pause:
            time.sleep(pause)

    collected.sort(key=lambda bar: bar.start)
    return collected


def fetch_and_store(
    symbol: str,
    destination: Path | str,
    *,
    since: int | None = None,
) -> int:
    bars = fetch_kraken_daily(symbol, since=since)
    if not bars:
        raise CryptoHistoryError(f"no bars returned for {symbol}")
    return save_bars(destination, bars)
