"""Historical OHLCV bars: fetch, parse, store, aggregate.

Bars come from the broker's ``get_equity_historicals`` tool, from an imported
CSV, or from recorded quotes. Payload parsing is fail-closed like the rest of
the broker layer: an unrecognised shape raises rather than inventing prices.
"""

from __future__ import annotations

import csv
import json
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any, Iterable, Optional

_BAR_LIST_KEYS = ("bars", "results", "data", "historicals", "candles", "items")
_START_KEYS = (
    "start_time",
    "start",
    "begins_at",
    "timestamp",
    "time",
    "date",
    "datetime",
    "observed_at",
    "quote_at",
)
_OPEN_KEYS = ("open_price", "open")
_HIGH_KEYS = ("high_price", "high")
_LOW_KEYS = ("low_price", "low")
_CLOSE_KEYS = ("close_price", "close")
_VOLUME_KEYS = ("volume", "volume_traded")


class BarParseError(RuntimeError):
    """Historical payload did not match any known shape (fail closed)."""


@dataclass(frozen=True)
class Bar:
    symbol: str
    start: datetime
    open: Decimal
    high: Decimal
    low: Decimal
    close: Decimal
    volume: Optional[Decimal] = None
    interpolated: bool = False

    def to_record(self) -> dict[str, Any]:
        record: dict[str, Any] = {
            "symbol": self.symbol,
            "start": self.start.astimezone(timezone.utc).isoformat(),
            "open": str(self.open),
            "high": str(self.high),
            "low": str(self.low),
            "close": str(self.close),
        }
        if self.volume is not None:
            record["volume"] = str(self.volume)
        if self.interpolated:
            record["interpolated"] = True
        return record


def parse_bars_payload(payload: Any, *, symbol: str) -> list[Bar]:
    """Extract bars from a ``get_equity_historicals`` payload."""
    entries = _find_entries(payload)
    if entries is None:
        keys = list(payload)[:12] if isinstance(payload, dict) else type(payload).__name__
        raise BarParseError(
            f"unrecognised historicals payload for {symbol} (keys: {keys}); "
            "run 'agentic-trading fetch-history --print-shape' to inspect it"
        )

    bars: list[Bar] = []
    for entry in entries:
        start = _time_of(entry)
        open_ = _decimal_of(entry, _OPEN_KEYS)
        high = _decimal_of(entry, _HIGH_KEYS)
        low = _decimal_of(entry, _LOW_KEYS)
        close = _decimal_of(entry, _CLOSE_KEYS)
        if start is None or None in (open_, high, low, close):
            continue
        bars.append(
            Bar(
                symbol=symbol.upper(),
                start=start,
                open=open_,  # type: ignore[arg-type]
                high=high,  # type: ignore[arg-type]
                low=low,  # type: ignore[arg-type]
                close=close,  # type: ignore[arg-type]
                volume=_decimal_of(entry, _VOLUME_KEYS),
                interpolated=bool(entry.get("interpolated")),
            )
        )
    bars.sort(key=lambda bar: bar.start)
    return bars


def _find_entries(payload: Any) -> Optional[list[dict[str, Any]]]:
    if isinstance(payload, list):
        items = [item for item in payload if isinstance(item, dict)]
        return items or None
    if not isinstance(payload, dict):
        return None
    for key in _BAR_LIST_KEYS:
        value = payload.get(key)
        if isinstance(value, list):
            items = [item for item in value if isinstance(item, dict)]
            if items and _looks_like_bar(items[0]):
                return items
            # Live shape wraps bars per symbol:
            # {"data": {"results": [{"symbol": ..., "bars": [...]}]}}
            nested_bars: list[dict[str, Any]] = []
            for item in items:
                for sub_key in _BAR_LIST_KEYS:
                    sub = item.get(sub_key)
                    if isinstance(sub, list):
                        nested_bars.extend(
                            bar for bar in sub if isinstance(bar, dict)
                        )
            if nested_bars:
                return nested_bars
        if isinstance(value, dict):
            nested = _find_entries(value)
            if nested:
                return nested
    # A single bar object.
    if any(k in payload for k in _CLOSE_KEYS) and any(k in payload for k in _START_KEYS):
        return [payload]
    return None


def _looks_like_bar(entry: dict[str, Any]) -> bool:
    return any(k in entry for k in _CLOSE_KEYS) and any(
        k in entry for k in _START_KEYS
    )


def _decimal_of(entry: dict[str, Any], keys: Iterable[str]) -> Optional[Decimal]:
    for key in keys:
        if key not in entry:
            continue
        value = entry[key]
        if isinstance(value, bool) or value is None:
            continue
        try:
            result = Decimal(str(value))
        except (InvalidOperation, ValueError):
            continue
        if result.is_finite() and result > 0:
            return result
    return None


def _time_of(entry: dict[str, Any]) -> Optional[datetime]:
    for key in _START_KEYS:
        if key not in entry:
            continue
        raw = entry[key]
        if isinstance(raw, (int, float)) and not isinstance(raw, bool):
            try:
                return datetime.fromtimestamp(float(raw), tz=timezone.utc)
            except (OverflowError, OSError, ValueError):
                continue
        if isinstance(raw, str) and raw.strip():
            text = raw.strip()
            try:
                parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
            except ValueError:
                try:
                    return datetime.fromtimestamp(float(text), tz=timezone.utc)
                except (OverflowError, OSError, ValueError):
                    continue
            if parsed.tzinfo is None:
                parsed = parsed.replace(tzinfo=timezone.utc)
            return parsed.astimezone(timezone.utc)
    return None


# --------------------------------------------------------------------------
# storage
# --------------------------------------------------------------------------


def save_bars(path: Path | str, bars: Iterable[Bar]) -> int:
    dest = Path(path)
    dest.parent.mkdir(parents=True, exist_ok=True)
    count = 0
    with dest.open("w", encoding="utf-8") as handle:
        for bar in bars:
            handle.write(json.dumps(bar.to_record(), separators=(",", ":")) + "\n")
            count += 1
    return count


def load_bars(path: Path | str) -> list[Bar]:
    source = Path(path)
    if not source.is_file():
        raise FileNotFoundError(f"no bar file at {source}")
    bars: list[Bar] = []
    for line in source.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        raw = json.loads(line)
        start = _time_of(raw)
        if start is None:
            continue
        open_ = _decimal_of(raw, _OPEN_KEYS)
        high = _decimal_of(raw, _HIGH_KEYS)
        low = _decimal_of(raw, _LOW_KEYS)
        close = _decimal_of(raw, _CLOSE_KEYS)
        if None in (open_, high, low, close):
            continue
        bars.append(
            Bar(
                symbol=str(raw.get("symbol", "")).upper(),
                start=start,
                open=open_,  # type: ignore[arg-type]
                high=high,  # type: ignore[arg-type]
                low=low,  # type: ignore[arg-type]
                close=close,  # type: ignore[arg-type]
                volume=_decimal_of(raw, _VOLUME_KEYS),
                interpolated=bool(raw.get("interpolated")),
            )
        )
    bars.sort(key=lambda bar: bar.start)
    return bars


def load_bars_csv(path: Path | str, *, symbol: str) -> list[Bar]:
    """Import CSV with date/time,open,high,low,close[,volume] columns."""
    source = Path(path)
    bars: list[Bar] = []
    with source.open(encoding="utf-8", newline="") as handle:
        reader = csv.DictReader(handle)
        for row in reader:
            normalized = {
                str(key).strip().lower(): value for key, value in row.items() if key
            }
            start = _time_of(normalized)
            open_ = _decimal_of(normalized, _OPEN_KEYS)
            high = _decimal_of(normalized, _HIGH_KEYS)
            low = _decimal_of(normalized, _LOW_KEYS)
            close = _decimal_of(normalized, _CLOSE_KEYS)
            if start is None or None in (open_, high, low, close):
                continue
            bars.append(
                Bar(
                    symbol=symbol.upper(),
                    start=start,
                    open=open_,  # type: ignore[arg-type]
                    high=high,  # type: ignore[arg-type]
                    low=low,  # type: ignore[arg-type]
                    close=close,  # type: ignore[arg-type]
                    volume=_decimal_of(normalized, _VOLUME_KEYS),
                )
            )
    bars.sort(key=lambda bar: bar.start)
    return bars


def bars_from_quotes(path: Path | str) -> list[Bar]:
    """Convert recorded bid/ask observations into one bar per observation.

    Used to exercise the evaluation pipeline on the repo's existing recordings.
    Each observation becomes a flat bar whose open/high/low/close is the
    midpoint, which understates intrabar range — documented, not hidden.
    """
    source = Path(path)
    if not source.is_file():
        raise FileNotFoundError(f"no quote file at {source}")
    bars: list[Bar] = []
    for line in source.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        raw = json.loads(line)
        start = _time_of(raw)
        bid = _decimal_of(raw, ("bid",))
        ask = _decimal_of(raw, ("ask",))
        if start is None or bid is None or ask is None:
            continue
        mid = (bid + ask) / 2
        bars.append(
            Bar(
                symbol=str(raw.get("symbol", "")).upper(),
                start=start,
                open=mid,
                high=mid,
                low=mid,
                close=mid,
            )
        )
    bars.sort(key=lambda bar: bar.start)
    return bars


def aggregate_bars(bars: list[Bar], *, minutes: int) -> list[Bar]:
    """Aggregate bars into fixed ``minutes`` buckets (the server cannot)."""
    if minutes <= 0:
        raise ValueError("minutes must be positive")
    bucket = timedelta(minutes=minutes)
    out: list[Bar] = []
    current: list[Bar] = []
    current_start: Optional[datetime] = None

    def flush() -> None:
        if not current:
            return
        out.append(
            Bar(
                symbol=current[0].symbol,
                start=current_start or current[0].start,  # type: ignore[arg-type]
                open=current[0].open,
                high=max(bar.high for bar in current),
                low=min(bar.low for bar in current),
                close=current[-1].close,
                volume=(
                    sum(
                        (bar.volume for bar in current if bar.volume is not None),
                        Decimal("0"),
                    )
                    if any(bar.volume is not None for bar in current)
                    else None
                ),
                interpolated=all(bar.interpolated for bar in current),
            )
        )

    for bar in bars:
        if current_start is None:
            current_start = bar.start
        if bar.start - current_start >= bucket:
            flush()
            current = []
            current_start = bar.start
        current.append(bar)
    flush()
    return out
