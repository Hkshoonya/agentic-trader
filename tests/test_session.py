"""US equity session calendar and session-policy tests."""

from __future__ import annotations

import unittest
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

from agentic_trading.session import (
    AFTERHOURS,
    HOLIDAY,
    OVERNIGHT,
    PREMARKET,
    REGULAR,
    WEEKEND,
    market_hours_argument,
    next_session_open,
    session_allows,
    session_for,
)

ET = ZoneInfo("America/New_York")


def et(year: int, month: int, day: int, hour: int, minute: int = 0) -> datetime:
    return datetime(year, month, day, hour, minute, tzinfo=ET)


class SessionDetectionTests(unittest.TestCase):
    def test_regular_hours_on_a_wednesday(self) -> None:
        self.assertEqual(session_for(et(2026, 9, 16, 10, 0)), REGULAR)
        self.assertEqual(session_for(et(2026, 9, 16, 15, 59)), REGULAR)

    def test_premarket_afterhours_and_overnight(self) -> None:
        self.assertEqual(session_for(et(2026, 9, 16, 6, 0)), PREMARKET)
        self.assertEqual(session_for(et(2026, 9, 16, 17, 0)), AFTERHOURS)
        self.assertEqual(session_for(et(2026, 9, 16, 22, 0)), OVERNIGHT)
        self.assertEqual(session_for(et(2026, 9, 16, 2, 0)), OVERNIGHT)

    def test_weekend(self) -> None:
        self.assertEqual(session_for(et(2026, 9, 19, 12, 0)), WEEKEND)  # Saturday
        self.assertEqual(session_for(et(2026, 9, 20, 12, 0)), WEEKEND)  # Sunday

    def test_sunday_night_reopens_overnight(self) -> None:
        self.assertEqual(session_for(et(2026, 9, 20, 21, 0)), OVERNIGHT)

    def test_holiday_is_closed(self) -> None:
        self.assertEqual(session_for(et(2026, 11, 26, 12, 0)), HOLIDAY)
        self.assertEqual(session_for(et(2026, 7, 3, 12, 0)), HOLIDAY)

    def test_early_close_moves_to_afterhours(self) -> None:
        self.assertEqual(session_for(et(2026, 11, 27, 12, 0)), REGULAR)
        self.assertEqual(session_for(et(2026, 11, 27, 14, 0)), AFTERHOURS)

    def test_naive_timestamp_is_treated_as_utc(self) -> None:
        # 14:00 UTC == 10:00 ET during EDT
        self.assertEqual(session_for(datetime(2026, 9, 16, 14, 0)), REGULAR)
        self.assertEqual(
            session_for(datetime(2026, 9, 16, 14, 0, tzinfo=timezone.utc)),
            REGULAR,
        )


class PolicyTests(unittest.TestCase):
    def test_regular_policy_blocks_extended_sessions(self) -> None:
        self.assertTrue(session_allows("regular", REGULAR))
        self.assertFalse(session_allows("regular", PREMARKET))
        self.assertFalse(session_allows("regular", AFTERHOURS))
        self.assertFalse(session_allows("regular", OVERNIGHT))
        self.assertFalse(session_allows("regular", WEEKEND))

    def test_extended_policy_allows_pre_and_post_but_not_overnight(self) -> None:
        self.assertTrue(session_allows("extended", PREMARKET))
        self.assertTrue(session_allows("extended", REGULAR))
        self.assertTrue(session_allows("extended", AFTERHOURS))
        self.assertFalse(session_allows("extended", OVERNIGHT))

    def test_all_policy_allows_overnight(self) -> None:
        self.assertTrue(session_allows("all", OVERNIGHT))
        self.assertFalse(session_allows("all", WEEKEND))

    def test_any_policy_is_explicit_opt_out(self) -> None:
        self.assertTrue(session_allows("any", WEEKEND))
        self.assertTrue(session_allows("any", HOLIDAY))

    def test_unknown_policy_raises(self) -> None:
        with self.assertRaises(ValueError):
            session_allows("sometimes", REGULAR)

    def test_market_hours_argument_mapping(self) -> None:
        self.assertEqual(market_hours_argument(REGULAR), "regular_hours")
        self.assertEqual(market_hours_argument(PREMARKET), "extended_hours")
        self.assertEqual(market_hours_argument(AFTERHOURS), "extended_hours")
        self.assertEqual(market_hours_argument(OVERNIGHT), "all_day_hours")
        with self.assertRaises(ValueError):
            market_hours_argument(WEEKEND)


class NextOpenTests(unittest.TestCase):
    def test_same_day_before_open(self) -> None:
        nxt = next_session_open(et(2026, 9, 16, 8, 0))
        self.assertEqual((nxt.hour, nxt.minute), (9, 30))
        self.assertEqual(nxt.date().isoformat(), "2026-09-16")

    def test_after_close_rolls_to_next_weekday(self) -> None:
        nxt = next_session_open(et(2026, 9, 16, 17, 0))
        self.assertEqual(nxt.date().isoformat(), "2026-09-17")

    def test_friday_evening_rolls_to_monday(self) -> None:
        nxt = next_session_open(et(2026, 9, 18, 18, 0))
        self.assertEqual(nxt.date().isoformat(), "2026-09-21")

    def test_holiday_is_skipped(self) -> None:
        nxt = next_session_open(et(2026, 11, 25, 18, 0))
        self.assertEqual(nxt.date().isoformat(), "2026-11-27")


if __name__ == "__main__":
    unittest.main()
