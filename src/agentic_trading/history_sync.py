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
from dataclasses import dataclass, field
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
    issues: list[str] = field(default_factory=list)
    volume_usable: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "symbol": self.symbol,
            "added": self.added,
            "total": self.total,
            "last_start": self.last_start,
            "source": self.source,
            "issues": list(self.issues),
            "volume_usable": self.volume_usable,
        }


@dataclass(frozen=True)
class QualityReport:
    """What is wrong with a bar file, before anything is allowed to learn from it."""

    symbol: str
    bars: int
    first_start: str
    last_start: str
    volume_coverage: float
    issues: list[str]

    @property
    def volume_usable(self) -> bool:
        return self.volume_coverage >= VOLUME_COVERAGE_FLOOR

    def to_dict(self) -> dict[str, Any]:
        return {
            "symbol": self.symbol,
            "bars": self.bars,
            "first_start": self.first_start[:10],
            "last_start": self.last_start[:10],
            "volume_coverage": round(self.volume_coverage, 3),
            "volume_usable": self.volume_usable,
            "issues": list(self.issues),
        }


# Below this share of bars carrying real volume, volume features are dropped for
# that symbol: a mostly-zero volume column is a data artefact, and a z-score
# computed on it is noise dressed as signal.
VOLUME_COVERAGE_FLOOR = 0.8
# A single-bar move this large is a data error far more often than a real move —
# unless it came with volume. Litecoin's 2017-03-30 (+83%) and 2017-12-12 (+61%)
# are real rallies on 10x normal volume, and flagging them forever is how a
# monitor trains its reader to ignore it.
SPIKE_PCT = 60.0
SPIKE_VOLUME_RATIO = 1.25


def check_records(symbol: str, records: list[dict[str, Any]]) -> QualityReport:
    """Validate a bar file: ordering, duplicates, spikes, gaps, volume coverage."""
    issues: list[str] = []
    if not records:
        return QualityReport(symbol, 0, "", "", 0.0, ["no bars"])

    starts = [str(record.get("start") or "") for record in records]
    if any(not start for start in starts):
        issues.append("bars without a start timestamp")
    ordered = [start for start in starts if start]
    if ordered != sorted(ordered):
        issues.append("bars are not in chronological order")
    duplicates = len(ordered) - len(set(ordered))
    if duplicates:
        issues.append(f"{duplicates} duplicate timestamps")

    closes: list[float] = []
    volumes: list[float] = []
    for record in records:
        try:
            closes.append(float(record["close"]))
        except (KeyError, TypeError, ValueError):
            continue
        try:
            volumes.append(float(record.get("volume") or 0.0))
        except (TypeError, ValueError):
            volumes.append(0.0)

    if any(close <= 0 for close in closes):
        issues.append("non-positive prices")
    spikes = []
    for index in range(1, len(closes)):
        if closes[index - 1] <= 0:
            continue
        moved = abs(closes[index] / closes[index - 1] - 1.0) * 100.0
        if moved <= SPIKE_PCT:
            continue
        window = [value for value in volumes[max(0, index - 20) : index] if value > 0]
        typical = sorted(window)[len(window) // 2] if window else 0.0
        volume_here = volumes[index] if index < len(volumes) else 0.0
        if typical > 0 and volume_here >= typical * SPIKE_VOLUME_RATIO:
            continue  # a big move that traded is a big move, not a bad print
        spikes.append(index)
    if spikes:
        issues.append(f"{len(spikes)} single-bar moves over {SPIKE_PCT:.0f}%")

    gaps = 0
    for index in range(1, len(starts)):
        try:
            left = datetime.fromisoformat(starts[index - 1])
            right = datetime.fromisoformat(starts[index])
        except ValueError:
            continue
        # Crypto trades every day; equities pause for weekends. Allow a week so
        # only genuine holes are flagged.
        if (right - left).days > 7:
            gaps += 1
    if gaps:
        issues.append(f"{gaps} gaps longer than a week")

    coverage = (
        sum(1 for volume in volumes if volume > 0) / len(volumes) if volumes else 0.0
    )
    if not volumes:
        issues.append("no volume column")
    elif coverage < VOLUME_COVERAGE_FLOOR:
        issues.append(f"volume present on only {coverage:.0%} of bars")

    return QualityReport(
        symbol=symbol,
        bars=len(records),
        first_start=ordered[0] if ordered else "",
        last_start=ordered[-1] if ordered else "",
        volume_coverage=coverage,
        issues=issues,
    )


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
    # Compact, matching ``history.save_bars``. The bar files are committed data,
    # and a sync that rewrites every line with different spacing turns a
    # two-bar append into a four-thousand-line diff.
    text = "\n".join(
        json.dumps(record, separators=(",", ":")) for record in records
    ) + "\n"
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


def fetch_crypto_records(
    symbol: str,
    *,
    timeout: float = 20.0,
    start: Optional[datetime] = None,
    end: Optional[datetime] = None,
) -> list[dict[str, Any]]:
    """Daily candles for ``symbol``, optionally a specific window.

    Coinbase returns at most 300 candles per call, so a window is how older
    history is paged back in.
    """
    import httpx

    product = symbol if "-" in symbol else f"{symbol[:-3]}-USD"
    params: dict[str, Any] = {"granularity": 86_400}
    if start is not None:
        params["start"] = start.astimezone(timezone.utc).isoformat()
    if end is not None:
        params["end"] = end.astimezone(timezone.utc).isoformat()
    response = httpx.get(
        COINBASE_CANDLES_URL.format(product=product.upper()),
        params=params,
        headers={"User-Agent": "agentic-trading/0.1"},
        timeout=timeout,
    )
    response.raise_for_status()
    payload = response.json()
    if not isinstance(payload, list):
        raise ValueError(f"unexpected candles payload for {product}")
    records = [coinbase_row_to_record(symbol, row) for row in payload]
    return [record for record in records if record is not None]


def backfill_crypto_volume(
    path: Path | str,
    *,
    symbol: str,
    fetch: Optional[Callable[..., list[dict[str, Any]]]] = None,
    max_pages: int = 12,
) -> int:
    """Fill in volume for older bars that were imported without it.

    Four crypto files carry a year or more of bars with no volume column at all
    (the source they came from did not include it). The live features only read
    the recent window, but the backtests read everything, and a volume column
    that is empty for a year is a trap waiting for the first genome that uses it.

    Returns how many bars gained volume. Pages backward until it runs out of
    history or hits ``max_pages``.
    """
    file = Path(path)
    records = read_records(file)
    if not records:
        return 0
    missing = [
        record
        for record in records
        if record.get("volume") in (None, "", "0", "0.0")
    ]
    if not missing:
        return 0

    fetch = fetch or fetch_crypto_records
    missing_dates = sorted(datetime.fromisoformat(str(record["start"])) for record in missing)
    # Walk backwards from the *newest* hole: the API serves 300 candles ending
    # at the window end, so paging from the old end of the file would fetch
    # windows that no longer contain the bars that are missing.
    cursor = missing_dates[-1] + timedelta(days=1)
    oldest_needed = missing_dates[0]
    known = {str(record["start"]): record for record in records}
    filled = 0
    for _ in range(max_pages):
        if cursor <= oldest_needed:
            break
        window_end = cursor
        window_start = window_end - timedelta(days=299)
        try:
            fetched = fetch(symbol, start=window_start, end=window_end)
        except Exception:  # noqa: BLE001 — a backfill is best effort
            break
        if not fetched:
            break
        for record in fetched:
            key = str(record.get("start"))
            volume = record.get("volume")
            existing = known.get(key)
            if existing is None:
                continue
            if existing.get("volume") in (None, "", "0", "0.0") and volume:
                existing["volume"] = volume
                filled += 1
        cursor = window_start

    if filled:
        merged, _ = merge_records(records, records)
        write_records(file, merged)
    return filled


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
    # Only touch the file when the contents actually change: rewriting it
    # moves the mtime, which changes the fingerprint, which makes the daemon
    # re-run a minutes-long search over identical data.
    if added or len(merged) != len(existing):
        write_records(file, merged)
    report = check_records(symbol, merged)
    return SyncResult(
        symbol=symbol,
        path=str(file),
        added=added,
        total=len(merged),
        last_start=str(merged[-1]["start"]) if merged else "",
        source="fetch",
        issues=report.issues,
        volume_usable=report.volume_usable,
    )


def quality_for(path: Path | str, *, symbol: str) -> QualityReport:
    return check_records(symbol, read_records(path))


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
    # The effective universe: a symbol the scout adopted needs its bars kept
    # current exactly like the operator's own, or the self-check fails on it and
    # the book disarms itself.
    wanted = list(symbols if symbols is not None else config.effective_whitelist)
    fetch_crypto = crypto_fetch or fetch_crypto_records

    results: list[SyncResult] = []
    errors: list[str] = []
    quality: list[QualityReport] = []
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
            # A file whose older bars were imported without volume gets a
            # bounded repair here, so the gap closes by itself instead of
            # warning forever.
            report = check_records(symbol, read_records(path))
            if is_crypto_symbol(symbol) and not report.volume_usable:
                gained = backfill_crypto_volume(
                    path, symbol=symbol, fetch=fetch_crypto, max_pages=4
                )
                if gained:
                    report = check_records(symbol, read_records(path))
                    result = SyncResult(
                        symbol=result.symbol,
                        path=result.path,
                        added=result.added,
                        total=report.bars,
                        last_start=report.last_start,
                        source=result.source,
                        issues=report.issues,
                        volume_usable=report.volume_usable,
                    )
            results.append(result)
            quality.append(report)
    if quality:
        try:
            jsonio.write_text(
                Path(config.state_dir) / "history_quality.json",
                jsonio.dumps(
                    {"reports": [report.to_dict() for report in quality]}, indent=2
                )
                + "\n",
            )
        except OSError:
            pass
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
