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
    def __init__(self, directory: Path | str) -> None:
        self.directory = Path(directory)
        self._last_error_at: Optional[datetime] = None

    def record(
        self, quotes: list[dict[str, Any]], *, now: Optional[datetime] = None
    ) -> Optional[str]:
        """Append ``quotes``; return an error message at most once an hour."""
        current = now or datetime.now(timezone.utc)
        batches: dict[Path, list[str]] = {}
        for quote in quotes:
            symbol = str(quote.get("symbol") or "").upper()
            if not _SYMBOL.match(symbol):
                continue
            day = _day(quote.get("observed_at"), current)
            row = {name: _text(quote.get(name)) for name in FIELDS}
            row["symbol"] = symbol
            path = self.directory / symbol / f"{day}.jsonl.gz"
            batches.setdefault(path, []).append(
                json.dumps(row, separators=(",", ":")) + "\n"
            )
        try:
            for path, lines in batches.items():
                path.parent.mkdir(parents=True, exist_ok=True)
                # One gzip member per batch: a crash can truncate at most the
                # batch being written, and gzip readers concatenate members.
                with gzip.open(path, "at", encoding="utf-8") as handle:
                    handle.writelines(lines)
        except OSError as exc:
            if (
                self._last_error_at is None
                or current - self._last_error_at >= _REPORT_EVERY
            ):
                self._last_error_at = current
                return f"{type(exc).__name__}: {exc}"[:200]
        return None


def _text(value: Any) -> Optional[str]:
    return None if value is None else str(value)


def _day(raw: Any, fallback: datetime) -> str:
    try:
        stamp = datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        stamp = fallback
    if stamp.tzinfo is None:
        stamp = stamp.replace(tzinfo=timezone.utc)
    return stamp.astimezone(timezone.utc).date().isoformat()
