"""Timezone lookups that work on a machine with no tz database.

Linux and macOS ship one; Windows does not, and Python's ``zoneinfo`` then
raises ``ZoneInfoNotFoundError`` unless the ``tzdata`` package is installed. The
frozen Windows build caught this the hard way: the app died at import with a
traceback no user could act on.

Two lines of defence, because "it works on my machine" is exactly the bug:

1. ``tzdata`` is a declared dependency on Windows, and the packaging spec bundles
   it, so the zone is normally found.
2. ``zone()`` never raises. If a zone is genuinely unavailable it falls back to
   the platform's current local offset and records the substitution, so the
   agent keeps trading on the right *session* boundary for that machine instead
   of failing to start. The fallback is logged once per zone, because a silent
   timezone is a quietly wrong trading calendar.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone, tzinfo
from functools import lru_cache
from typing import Optional

LOGGER = logging.getLogger("agentic_trading.tz")

# Fixed-offset fallbacks for the zones this project actually asks for. Used only
# when the platform has no database: a market-hours calendar pinned to the wrong
# UTC offset is worse than one pinned to the right one.
_KNOWN_OFFSETS: dict[str, tuple[int, int]] = {
    "America/New_York": (-5, -4),
    "UTC": (0, 0),
}

_WARNED: set[str] = set()


class _Fallback(tzinfo):
    """A fixed offset that still reports the zone name it stands in for."""

    def __init__(self, name: str, offset: timedelta) -> None:
        self._name = name
        self._offset = offset

    def utcoffset(self, dt: Optional[datetime]) -> timedelta:  # noqa: D102
        return self._offset

    def dst(self, dt: Optional[datetime]) -> timedelta:  # noqa: D102
        return timedelta(0)

    def tzname(self, dt: Optional[datetime]) -> str:  # noqa: D102
        return self._name

    def __repr__(self) -> str:  # pragma: no cover — debugging aid
        return f"Fallback({self._name}, {self._offset})"


def _warn(name: str, detail: str) -> None:
    if name in _WARNED:
        return
    _WARNED.add(name)
    LOGGER.warning(
        "timezone %s is unavailable (%s); falling back to the platform's "
        "current local offset",
        name,
        detail,
    )


def _current_offset() -> timedelta:
    """Whatever offset this machine is on right now, DST included."""
    return datetime.now().astimezone().utcoffset() or timedelta(0)


@lru_cache(maxsize=16)
def zone(name: str) -> tzinfo:
    """A ``tzinfo`` for ``name``; never raises."""
    from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

    try:
        return ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError, KeyError) as exc:
        _warn(name, type(exc).__name__)
        if name in _KNOWN_OFFSETS:
            # Choose the offset that applies today, so the trading calendar is
            # right for the season the machine is actually in.
            winter, summer = _KNOWN_OFFSETS[name]
            local = _current_offset().total_seconds() / 3600
            hours = summer if local in (summer, summer - 1, summer + 1) else winter
            return _Fallback(name, timedelta(hours=hours))
        return _Fallback(name, _current_offset())


def eastern() -> tzinfo:
    """US equity session timezone (America/New_York)."""
    return zone("America/New_York")


def utc() -> tzinfo:
    return timezone.utc
