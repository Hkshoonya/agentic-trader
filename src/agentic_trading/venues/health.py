"""How each stream and venue is doing, written once a second for the console."""

from __future__ import annotations

import statistics
from collections import deque
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Callable, Optional

from agentic_trading import jsonio
from agentic_trading.venues.model import Tick

WINDOW = timedelta(seconds=60)
_STICKY = ("auth_failed", "off", "reconnecting")


class StreamHealth:
    def __init__(self, key: str, venue: str, *, always_open: bool) -> None:
        self.key = key
        self.venue = venue
        self.always_open = always_open
        self.status = "starting"
        self.connected = False
        self.reconnects = 0
        self.last_error = ""
        self.last_tick_at: Optional[datetime] = None
        self.started_at: Optional[datetime] = None
        self.skew = 0
        self._delays: deque[tuple[datetime, float]] = deque(maxlen=5000)

    def begin(self, now: datetime) -> None:
        """A connection attempt starts; staleness is measured from here until a tick."""
        self.status = "starting"
        self.started_at = now

    def on_tick(self, tick: Tick) -> None:
        ms = (tick.received_at - tick.exchange_at).total_seconds() * 1000
        if ms < 0:
            # The exchange clock is ahead of ours: count it, never report it as
            # a negative delay.
            self.skew += 1
            ms = 0.0
        self._delays.append((tick.received_at, ms))
        self.last_tick_at = tick.received_at
        self.connected = True
        self.status = "live"

    def delay_ms(self, now: datetime) -> tuple[Optional[float], Optional[float]]:
        recent = sorted(ms for at, ms in self._delays if now - at <= WINDOW)
        if not recent:
            return None, None
        p95 = recent[int(0.95 * (len(recent) - 1))]
        return statistics.median(recent), p95

    def evaluate(self, now: datetime, *, stale_after: float, market_open: bool) -> None:
        if self.status in _STICKY:
            return
        if not market_open:
            self.status = "closed"  # a quiet stock stream after hours is expected
            return
        ticked = self.last_tick_at is not None and (
            self.started_at is None or self.last_tick_at >= self.started_at
        )
        if ticked:
            quiet = (now - self.last_tick_at).total_seconds()  # type: ignore[operator]
            self.status = "stale" if quiet > stale_after else "live"
        elif self.started_at is not None:
            # Connected (or trying) but silent since this attempt began.
            waited = (now - self.started_at).total_seconds()
            self.status = "stale" if waited > stale_after else "starting"

    def snapshot(self, now: datetime) -> dict[str, Any]:
        median, p95 = self.delay_ms(now)
        age = None if self.last_tick_at is None else round((now - self.last_tick_at).total_seconds(), 1)
        return {
            "key": self.key,
            "venue": self.venue,
            "status": self.status,
            "connected": self.connected,
            "last_tick_age_s": age,
            "delay_ms_median": None if median is None else round(median, 1),
            "delay_ms_p95": None if p95 is None else round(p95, 1),
            "reconnects": self.reconnects,
            "skew": self.skew,
            "last_error": self.last_error,
        }


class HealthBoard:
    def __init__(self) -> None:
        self.streams: dict[str, StreamHealth] = {}
        self.venues: dict[str, dict[str, Any]] = {}
        self.latest: dict[str, dict[str, Optional[str]]] = {}

    def stream(self, key: str, venue: str, *, always_open: bool) -> StreamHealth:
        if key not in self.streams:
            self.streams[key] = StreamHealth(key, venue, always_open=always_open)
        return self.streams[key]

    def on_tick(self, key: str, tick: Tick) -> None:
        stream = self.streams.get(key)
        if stream is not None:
            stream.on_tick(tick)
        row = tick.to_row()
        self.latest[f"{tick.venue}:{tick.symbol}"] = {
            "bid": row["bid"], "ask": row["ask"], "last": row["last"], "at": row["received_at"],
        }

    def set_venue(self, name: str, **fields: Any) -> None:
        self.venues.setdefault(name, {"name": name}).update(fields)

    def evaluate(
        self,
        now: datetime,
        *,
        stale_after: float,
        market_open: Callable[[StreamHealth, datetime], bool],
    ) -> None:
        for stream in self.streams.values():
            stream.evaluate(now, stale_after=stale_after, market_open=market_open(stream, now))

    def snapshot(self, now: datetime) -> dict[str, Any]:
        return {
            "as_of": now.isoformat(),
            "streams": [s.snapshot(now) for s in self.streams.values()],
            "venues": [dict(v) for v in self.venues.values()],
            "latest": dict(self.latest),
        }

    def write(self, path: Path | str, now: datetime) -> None:
        target = Path(path)
        target.parent.mkdir(parents=True, exist_ok=True)
        jsonio.write_text(target, jsonio.dumps(self.snapshot(now), indent=1) + "\n")
