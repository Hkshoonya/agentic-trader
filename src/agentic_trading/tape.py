"""The quote tape: every polled quote, appended to a daily gzip file per symbol.

Backtests on the downloaded one-year 5-minute set were worthless: 43% of its
bars were interpolated flat fills. The daemon already sees a fresh quote about
every five seconds, so it records its own tape: the clean intraday data a fast
lane has to be designed on. Recording must never cost the trading loop
anything, so every failure is swallowed and reported at most once an hour.
"""

from __future__ import annotations

import gzip
import json
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Optional

FIELDS = ("symbol", "bid", "ask", "quote_at", "observed_at")
_SYMBOL = re.compile(r"^[A-Z0-9][A-Z0-9.\-]{0,15}$")
_REPORT_EVERY = timedelta(hours=1)


class QuoteTape:
    """Buffer quotes in memory and append them in chunks of ``flush_seconds``.

    One gzip member per poll barely compresses (a poll holds one row per
    symbol), which put the tape at ~35 MB a day. A minute of rows per member
    compresses several-fold; the cost is that a crash loses at most the last
    ``flush_seconds`` of quotes. The daemon flushes on the way out.
    """

    def __init__(self, directory: Path | str, *, flush_seconds: float = 60.0) -> None:
        self.directory = Path(directory)
        self.flush_seconds = float(flush_seconds)
        self._pending: dict[Path, list[str]] = {}
        self._last_flush_at: Optional[datetime] = None
        self._last_error_at: Optional[datetime] = None

    def record(
        self, quotes: list[dict[str, Any]], *, now: Optional[datetime] = None
    ) -> Optional[str]:
        """Buffer ``quotes``; flush when due. Returns an error at most once an hour."""
        current = now or datetime.now(timezone.utc)
        for quote in quotes:
            symbol = str(quote.get("symbol") or "").upper()
            if not _SYMBOL.match(symbol):
                continue
            day = _day(quote.get("observed_at"))
            if day is None:
                continue  # an untimed quote cannot be placed on the tape honestly
            row = {name: _text(quote.get(name)) for name in FIELDS}
            row["symbol"] = symbol
            path = self.directory / symbol / f"{day}.jsonl.gz"
            self._pending.setdefault(path, []).append(
                json.dumps(row, separators=(",", ":")) + "\n"
            )
        if self._last_flush_at is None:
            self._last_flush_at = current
        if (current - self._last_flush_at).total_seconds() >= self.flush_seconds:
            return self.flush(now=current)
        return None

    def flush(self, *, now: Optional[datetime] = None) -> Optional[str]:
        """Write every buffered row now. Returns an error at most once an hour."""
        current = now or datetime.now(timezone.utc)
        self._last_flush_at = current
        pending, self._pending = self._pending, {}
        try:
            for path, lines in pending.items():
                path.parent.mkdir(parents=True, exist_ok=True)
                # One gzip member per chunk: a crash can truncate at most the
                # chunk being written, and gzip readers concatenate members.
                with gzip.open(path, "at", encoding="utf-8") as handle:
                    handle.writelines(lines)
        except OSError as exc:
            # The rows are dropped rather than retried: a disk that refuses
            # writes must not also grow the daemon's memory without bound.
            if (
                self._last_error_at is None
                or current - self._last_error_at >= _REPORT_EVERY
            ):
                self._last_error_at = current
                return f"{type(exc).__name__}: {exc}"[:200]
        return None


def _text(value: Any) -> Optional[str]:
    return None if value is None else str(value)


def _day(raw: Any) -> Optional[str]:
    if raw is None:
        return None
    try:
        stamp = datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None
    if stamp.tzinfo is None:
        stamp = stamp.replace(tzinfo=timezone.utc)
    return stamp.astimezone(timezone.utc).date().isoformat()
