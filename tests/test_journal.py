# tests/test_journal.py
from pathlib import Path
import tempfile
import unittest

from agentic_trading.journal import DecisionJournal


class JournalTests(unittest.TestCase):
    def test_every_record_is_timestamped(self):
        """Without this you cannot measure how often the model was consulted."""
        with tempfile.TemporaryDirectory() as tmp:
            j = DecisionJournal(Path(tmp))
            j.append({"event": "advisor", "confidence": 0.6})
            record = list(j.iter_today())[0]
            self.assertIn("at", record)
            self.assertGreater(len(record["at"]), 10)

    def test_a_caller_supplied_timestamp_is_kept(self):
        with tempfile.TemporaryDirectory() as tmp:
            j = DecisionJournal(Path(tmp))
            j.append({"event": "custom", "at": "2026-01-01T00:00:00+00:00"})
            record = list(j.iter_today())[0]
            self.assertEqual(record["at"], "2026-01-01T00:00:00+00:00")

    def test_iter_today_empty_before_append(self):
        with tempfile.TemporaryDirectory() as tmp:
            j = DecisionJournal(Path(tmp))
            self.assertEqual(list(j.iter_today()), [])

    def test_append_and_has_decision(self):
        with tempfile.TemporaryDirectory() as tmp:
            j = DecisionJournal(Path(tmp))
            j.append({"decision_id": "abc", "event": "accepted"})
            self.assertTrue(j.has_decision("abc"))
            self.assertFalse(j.has_decision("nope"))
            lines = list(j.iter_today())
            self.assertEqual(len(lines), 1)
