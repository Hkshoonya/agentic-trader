"""US equity market sessions (America/New_York).

Sessions modelled: ``premarket``, ``regular``, ``afterhours``, ``overnight``,
plus ``weekend`` and ``holiday`` closures. The 24-hour (overnight) market is
approximated as Sunday 20:00 ET through Friday 20:00 ET.

An unknown or unverified session is treated as **not tradable** by every
session policy except ``any`` (an explicit operator override used for testing).
"""

from __future__ import annotations

from datetime import date, datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo

ET = ZoneInfo("America/New_York")

PREMARKET_OPEN = time(4, 0)
REGULAR_OPEN = time(9, 30)
REGULAR_CLOSE = time(16, 0)
AFTERHOURS_CLOSE = time(20, 0)

PREMARKET = "premarket"
REGULAR = "regular"
AFTERHOURS = "afterhours"
OVERNIGHT = "overnight"
WEEKEND = "weekend"
HOLIDAY = "holiday"

TRADABLE_SESSIONS = frozenset({PREMARKET, REGULAR, AFTERHOURS, OVERNIGHT})

POLICIES = ("regular", "extended", "all", "any")

# NYSE full-day closures, 2026.
HOLIDAYS_2026: frozenset[date] = frozenset(
    {
        date(2026, 1, 1),   # New Year's Day
        date(2026, 1, 19),  # Martin Luther King Jr. Day
        date(2026, 2, 16),  # Washington's Birthday
        date(2026, 4, 3),   # Good Friday
        date(2026, 5, 25),  # Memorial Day
        date(2026, 6, 19),  # Juneteenth
        date(2026, 7, 3),   # Independence Day (observed; Jul 4 is a Saturday)
        date(2026, 9, 7),   # Labor Day
        date(2026, 11, 26),  # Thanksgiving
        date(2026, 12, 25),  # Christmas
    }
)

# NYSE early closes (13:00 ET), 2026.
EARLY_CLOSES_2026: frozenset[date] = frozenset(
    {
        date(2026, 11, 27),  # day after Thanksgiving
        date(2026, 12, 24),  # Christmas Eve
    }
)


def _to_et(now: datetime) -> datetime:
    if now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)
    return now.astimezone(ET)


def session_for(
    now: datetime,
    *,
    holidays: frozenset[date] = HOLIDAYS_2026,
    early_closes: frozenset[date] = EARLY_CLOSES_2026,
) -> str:
    """Return the current session name for ``now`` (naive values treated as UTC)."""
    local = _to_et(now)
    day = local.date()
    clock = local.time()
    weekday = local.weekday()  # Monday == 0

    if day in holidays:
        return HOLIDAY

    # Saturday: before 04:00 ET is the tail of Friday's overnight session.
    if weekday == 5:
        return OVERNIGHT if clock < PREMARKET_OPEN else WEEKEND

    # Sunday: the 24-hour market reopens at 20:00 ET.
    if weekday == 6:
        return OVERNIGHT if clock >= AFTERHOURS_CLOSE else WEEKEND

    if clock < PREMARKET_OPEN:
        return OVERNIGHT

    if clock < REGULAR_OPEN:
        return PREMARKET

    close = time(13, 0) if day in early_closes else REGULAR_CLOSE
    if clock < close:
        return REGULAR

    if clock < AFTERHOURS_CLOSE:
        return AFTERHOURS

    return OVERNIGHT


def session_allows(policy: str, session: str) -> bool:
    """Whether ``policy`` permits trading in ``session``."""
    if policy == "regular":
        return session == REGULAR
    if policy == "extended":
        return session in (PREMARKET, REGULAR, AFTERHOURS)
    if policy == "all":
        return session in TRADABLE_SESSIONS
    if policy == "any":
        return True
    raise ValueError(f"unknown session policy: {policy!r}")


def market_hours_argument(session: str) -> str:
    """Map a session to the MCP ``market_hours`` argument."""
    if session == REGULAR:
        return "regular_hours"
    if session in (PREMARKET, AFTERHOURS):
        return "extended_hours"
    if session == OVERNIGHT:
        return "all_day_hours"
    raise ValueError(f"session {session!r} is not tradable")


def next_session_open(now: datetime, *, holidays: frozenset[date] = HOLIDAYS_2026) -> datetime:
    """Return the next regular-session open at or after ``now`` (ET-aware)."""
    local = _to_et(now)
    candidate = local.replace(
        hour=REGULAR_OPEN.hour, minute=REGULAR_OPEN.minute, second=0, microsecond=0
    )
    if candidate <= local:
        candidate = candidate + timedelta(days=1)
    for _ in range(10):
        if candidate.weekday() < 5 and candidate.date() not in holidays:
            return candidate
        candidate = (candidate + timedelta(days=1)).replace(
            hour=REGULAR_OPEN.hour, minute=REGULAR_OPEN.minute, second=0, microsecond=0
        )
    return candidate
