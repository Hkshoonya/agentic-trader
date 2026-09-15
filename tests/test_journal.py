# tests/test_journal.py
from pathlib import Path
import tempfile
import unittest

from agentic_trading.journal import DecisionJournal


class JournalTests(unittest.TestCase):
    def test_append_and_has_decision(self):
        with tempfile.TemporaryDirectory() as tmp:
            j = DecisionJournal(Path(tmp))
            j.append({"decision_id": "abc", "event": "accepted"})
            self.assertTrue(j.has_decision("abc"))
            self.assertFalse(j.has_decision("nope"))
            lines = list(j.iter_today())
            self.assertEqual(len(lines), 1)
