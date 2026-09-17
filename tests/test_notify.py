"""Operator alerts: reach a human on the events that matter, never spam, never block."""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from agentic_trading.notify import Alert, Notifier, alert_for
from agentic_trading.runtime import NotifyingJournal


class _Channel:
    def __init__(self, name: str, *, ok: bool = True) -> None:
        self.name = name
        self.ok = ok
        self.sent: list[Alert] = []

    def send(self, alert: Alert) -> bool:
        self.sent.append(alert)
        return self.ok


class _Journal:
    def __init__(self) -> None:
        self.records: list[dict] = []

    def append(self, record: dict) -> None:
        self.records.append(record)

    def iter_today(self):
        return iter(self.records)

    def events(self) -> list[str]:
        return [record.get("event") for record in self.records]


class AlertContentTests(unittest.TestCase):
    def test_promotion_and_kill_switch_are_alertable(self) -> None:
        promotion = alert_for({"event": "promotion", "from": "shadow", "to": "live"})
        self.assertIsNotNone(promotion)
        self.assertEqual(promotion.urgency, "normal")
        kill = alert_for({"event": "kill_switch", "reason": "consecutive_place_failed"})
        self.assertIsNotNone(kill)
        self.assertEqual(kill.urgency, "critical")
        self.assertIn("consecutive_place_failed", kill.body)

    def test_a_blocked_order_says_why(self) -> None:
        alert = alert_for(
            {
                "event": "live_gate_blocked",
                "symbol": "BTC-USD",
                "side": "buy",
                "notional": "1.61",
            }
        )
        self.assertIn("BTC-USD", alert.body)
        self.assertIn("$1.61", alert.body)
        self.assertIn("AGENTIC_ALLOW_LIVE", alert.body)

    def test_routine_events_are_silent(self) -> None:
        for event in ("resized", "advisor", "rejected", "cycle_stats", "regime"):
            self.assertIsNone(alert_for({"event": event}), event)


class NotifierTests(unittest.TestCase):
    def test_fans_out_to_every_channel(self) -> None:
        desktop, telegram = _Channel("desktop"), _Channel("telegram")
        notifier = Notifier([desktop, telegram])
        alert = notifier.dispatch({"event": "promotion", "to": "live"})
        self.assertTrue(alert.delivered)
        self.assertEqual(alert.channels, ["desktop", "telegram"])
        self.assertEqual(notifier.sent, 1)

    def test_a_dead_channel_does_not_stop_the_others(self) -> None:
        broken = _Channel("desktop", ok=False)
        telegram = _Channel("telegram")
        notifier = Notifier([broken, telegram])
        alert = notifier.dispatch({"event": "kill_switch", "reason": "x"})
        self.assertEqual(alert.channels, ["telegram"])
        self.assertEqual(notifier.channel_failures, {"desktop": 1})
        self.assertEqual(notifier.failures, 0)  # the alert still reached someone

    def test_a_raising_channel_is_survivable(self) -> None:
        class _Exploding:
            name = "boom"

            def send(self, alert: Alert) -> bool:
                raise RuntimeError("dbus gone")

        notifier = Notifier([_Exploding(), _Channel("telegram")])
        alert = notifier.dispatch({"event": "kill_switch", "reason": "x"})
        self.assertEqual(alert.channels, ["telegram"])

    def test_repeated_blocks_are_rate_limited(self) -> None:
        """A blocked entry every six seconds must not become a message every six seconds."""
        channel = _Channel("desktop")
        notifier = Notifier([channel])
        record = {
            "event": "live_gate_blocked",
            "symbol": "BTC-USD",
            "side": "buy",
            "notional": "1.61",
        }
        self.assertIsNotNone(notifier.dispatch(record))
        self.assertIsNone(notifier.dispatch(record))
        self.assertIsNone(notifier.dispatch(record))
        self.assertEqual(len(channel.sent), 1)

    def test_a_placed_order_always_alerts(self) -> None:
        channel = _Channel("desktop")
        notifier = Notifier([channel])
        for i in range(3):
            record = {
                "event": "placed",
                "decision_id": f"d-{i}",
                "order_request": {"symbol": "BTC-USD", "side": "buy", "dollar_amount": "1.61"},
            }
            self.assertIsNotNone(notifier.dispatch(record))
        self.assertEqual(len(channel.sent), 3)

    def test_no_channels_means_no_dispatch(self) -> None:
        self.assertIsNone(Notifier([]).dispatch({"event": "kill_switch"}))


class NotifyingJournalTests(unittest.TestCase):
    def test_alerts_ride_along_and_are_recorded(self) -> None:
        journal = _Journal()
        wrapper = NotifyingJournal(journal, Notifier([_Channel("desktop")]))
        wrapper.append({"event": "kill_switch", "reason": "consecutive_errors"})
        self.assertEqual(journal.events(), ["kill_switch", "notify"])
        notify = journal.records[-1]
        self.assertEqual(notify["channels"], ["desktop"])
        self.assertTrue(notify["delivered"])

    def test_delegates_everything_else_to_the_real_journal(self) -> None:
        journal = _Journal()
        wrapper = NotifyingJournal(journal, Notifier([_Channel("desktop")]))
        wrapper.append({"event": "advisor"})
        self.assertEqual(len(list(wrapper.iter_today())), 1)

    def test_a_failing_notifier_cannot_lose_a_decision(self) -> None:
        class _Broken:
            def dispatch(self, record):
                raise RuntimeError("notifier exploded")

        journal = _Journal()
        wrapper = NotifyingJournal(journal, _Broken())
        wrapper.append({"event": "accepted", "symbol": "BTC-USD"})
        self.assertEqual(journal.events(), ["accepted"])


if __name__ == "__main__":
    unittest.main()
