"""Keep the bar files current, so the evidence is allowed to change.

The self-evaluation grades the strategy on the bars in ``history_path``. Those
files were written once by hand, and nothing refreshed them — so every hourly
evaluation re-ran the same deterministic search over the same frozen data and
produced the same confidence, forever. The risk budget could never move and the
agent could never learn anything new.

This module closes that loop:

- **equities** come from the broker's own historicals tool (authenticated);
- **crypto** comes from Coinbase's public candles endpoint, because Robinhood
  exposes no crypto historicals tool at all;
- results are **merged** into the existing files keyed on bar start time, so a
  fetch that only returns the most recent window adds to history instead of
  truncating it.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Iterable, Optional

from agentic_trading import jsonio
from agentic_trading.orders import is_crypto_symbol

COINBASE_CANDLES_URL = "https://api.exchange.coinbase.com/products/{product}/candles"
# Coinbase returns at most 300 daily candles per call; a month is plenty for a
# daily-horizon refresh and keeps the request cheap.
CRYPTO_LOOKBACK_DAYS = 30
EQUITY_LOOKBACK_DAYS = 30


@dataclass(frozen=True)
class SyncResult:
    symbol: str
    path: str
    added: int
    total: int
    last_start: str
    source: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "symbol": self.symbol,
            "added": self.added,
            "total": self.total,
            "last_start": self.last_start,
            "source": self.source,
        }


def bar_stem(symbol: str) -> str:
    """``BTC-USD`` → ``BTCUSD``: bar files are written without the dash."""
    return symbol.replace("-", "").strip().upper()


def read_records(path: Path | str) -> list[dict[str, Any]]:
    file = Path(path)
    if not file.is_file():
        return []
    records: list[dict[str, Any]] = []
    for line in file.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            payload = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(payload, dict) and payload.get("start"):
            records.append(payload)
    return records


def merge_records(
    existing: Iterable[dict[str, Any]], incoming: Iterable[dict[str, Any]]
) -> tuple[list[dict[str, Any]], int]:
    """Union on bar start time, newest fetch winning for a shared timestamp.

    Returns the merged records (oldest first) and how many starts were new.
    """
    by_start: dict[str, dict[str, Any]] = {
        str(record["start"]): record for record in existing if record.get("start")
    }
    added = 0
    for record in incoming:
        start = record.get("start")
        if not start:
            continue
        key = str(start)
        if key not in by_start:
            added += 1
        by_start[key] = record
    ordered = [by_start[key] for key in sorted(by_start)]
    return ordered, added


def write_records(path: Path | str, records: Iterable[dict[str, Any]]) -> None:
    text = "\n".join(json.dumps(record) for record in records) + "\n"
    jsonio.write_text(path, text)


def coinbase_row_to_record(symbol: str, row: Any) -> Optional[dict[str, Any]]:
    """Coinbase candle ``[time, low, high, open, close, volume]`` → bar record."""
    try:
        epoch, low, high, open_, close, volume = row
        start = datetime.fromtimestamp(int(epoch), tz=timezone.utc)
    except (TypeError, ValueError):
        return None
    return {
        "symbol": bar_stem(symbol),
        "start": start.isoformat(),
        "open": str(open_),
        "high": str(high),
        "low": str(low),
        "close": str(close),
        "volume": str(volume),
    }


def fetch_crypto_records(symbol: str, *, timeout: float = 20.0) -> list[dict[str, Any]]:
    import httpx

    product = symbol if "-" in symbol else f"{symbol[:-3]}-USD"
    response = httpx.get(
        COINBASE_CANDLES_URL.format(product=product.upper()),
        params={"granularity": 86_400},
        headers={"User-Agent": "agentic-trading/0.1"},
        timeout=timeout,
    )
    response.raise_for_status()
    payload = response.json()
    if not isinstance(payload, list):
        raise ValueError(f"unexpected candles payload for {product}")
    records = [coinbase_row_to_record(symbol, row) for row in payload]
    return [record for record in records if record is not None]


def fetch_equity_records(broker: Any, symbol: str) -> list[dict[str, Any]]:
    from agentic_trading.history import parse_bars_payload

    start = (
        datetime.now(timezone.utc) - timedelta(days=EQUITY_LOOKBACK_DAYS)
    ).isoformat()
    payload = broker.get_historicals(
        [symbol], start_time=start, interval="day", bounds="regular"
    )
    return [bar.to_record() for bar in parse_bars_payload(payload, symbol=symbol)]


def sync_symbol(
    path: Path | str,
    *,
    symbol: str,
    fetch: Callable[[], list[dict[str, Any]]],
) -> Optional[SyncResult]:
    """Merge a fresh fetch into one bar file. Network errors propagate."""
    file = Path(path)
    incoming = fetch()
    if not incoming:
        return None
    existing = read_records(file)
    merged, added = merge_records(existing, incoming)
    write_records(file, merged)
    return SyncResult(
        symbol=symbol,
        path=str(file),
        added=added,
        total=len(merged),
        last_start=str(merged[-1]["start"]) if merged else "",
        source="fetch",
    )


def sync_history(
    config: Any,
    broker: Any,
    *,
    symbols: Optional[Iterable[str]] = None,
    crypto_fetch: Optional[Callable[[str], list[dict[str, Any]]]] = None,
) -> tuple[list[SyncResult], list[str]]:
    """Refresh every whitelisted symbol that has a bar file.

    Returns ``(results, errors)``. A single symbol failing never stops the rest:
    stale history for one instrument is not a reason to skip the others.
    """
    directory = Path(config.history_path) if config.history_path else None
    if directory is None:
        return [], ["no history_path configured"]
    wanted = list(symbols if symbols is not None else config.symbol_whitelist)
    fetch_crypto = crypto_fetch or fetch_crypto_records

    results: list[SyncResult] = []
    errors: list[str] = []
    for symbol in wanted:
        stem = bar_stem(symbol)
        path = directory / f"{stem}_day.jsonl"
        if not path.is_file():
            continue  # nothing to extend; the fetcher would be writing new history
        try:
            if is_crypto_symbol(symbol):
                result = sync_symbol(
                    path, symbol=symbol, fetch=lambda s=symbol: fetch_crypto(s)
                )
            else:
                result = sync_symbol(
                    path,
                    symbol=symbol,
                    fetch=lambda s=symbol: fetch_equity_records(broker, s),
                )
        except Exception as exc:  # noqa: BLE001 — one symbol must not stop the rest
            errors.append(f"{symbol}: {type(exc).__name__}: {exc}"[:200])
            continue
        if result is not None:
            results.append(result)
    return results, errors


def fingerprint(directory: Path | str, symbols: Iterable[str]) -> str:
    """Cheap identity for the bar files the evaluation would read.

    Used to skip an expensive re-run when nothing changed — which is also the
    honest explanation for "why did confidence not move".
    """
    parts: list[str] = []
    for symbol in sorted(bar_stem(s) for s in symbols):
        path = Path(directory) / f"{symbol}_day.jsonl"
        if not path.is_file():
            continue
        try:
            info = path.stat()
        except OSError:
            continue
        parts.append(f"{symbol}:{info.st_size}:{info.st_mtime_ns}")
    return "|".join(parts)
