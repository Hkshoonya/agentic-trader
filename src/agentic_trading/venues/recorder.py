"""Every venue tick, appended to a daily gzip file per venue and symbol.

The same buffering as the Robinhood quote tape: a minute of rows per gzip
member compresses well, and a crash loses at most the unflushed minute.
"""

from __future__ import annotations

import gzip
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional

from agentic_trading.venues.model import Tick

_REPORT_EVERY = timedelta(hours=1)


class StreamRecorder:
    def __init__(self, directory: Path | str, *, flush_seconds: float = 60.0) -> None:
        self.directory = Path(directory)
        self.flush_seconds = float(flush_seconds)
        self._pending: dict[Path, list[str]] = {}
        self._last_flush_at: Optional[datetime] = None
        self._last_error_at: Optional[datetime] = None

    def record(self, tick: Tick, *, now: Optional[datetime] = None) -> Optional[str]:
        current = now or datetime.now(timezone.utc)
        day = tick.received_at.astimezone(timezone.utc).date().isoformat()
        folder = tick.symbol.replace("/", "").replace("-", "")
        path = self.directory / tick.venue / folder / f"{day}.jsonl.gz"
        self._pending.setdefault(path, []).append(
            json.dumps(tick.to_row(), separators=(",", ":")) + "\n"
        )
        if self._last_flush_at is None:
            self._last_flush_at = current
        if (current - self._last_flush_at).total_seconds() >= self.flush_seconds:
            return self.flush(now=current)
        return None

    def flush(self, *, now: Optional[datetime] = None) -> Optional[str]:
        current = now or datetime.now(timezone.utc)
        self._last_flush_at = current
        pending, self._pending = self._pending, {}
        try:
            for path, lines in pending.items():
                path.parent.mkdir(parents=True, exist_ok=True)
                with gzip.open(path, "at", encoding="utf-8") as handle:
                    handle.writelines(lines)
        except OSError as exc:
            if self._last_error_at is None or current - self._last_error_at >= _REPORT_EVERY:
                self._last_error_at = current
                return f"{type(exc).__name__}: {exc}"[:200]
        return None
