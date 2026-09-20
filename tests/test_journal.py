# tests/test_journal.py
import os
from pathlib import Path
import stat
import tempfile
import unittest

from agentic_trading.journal import DecisionJournal


class JournalTests(unittest.TestCase):
    @unittest.skipIf(os.name == "nt", "POSIX mode bits are not portable to Windows")
    def test_new_journal_directory_and_file_are_owner_only(self):
        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp) / "private" / "journal"
            j = DecisionJournal(directory)
            j.append({"event": "accepted"})
            file = next(directory.iterdir())

            self.assertEqual(stat.S_IMODE(directory.stat().st_mode), 0o700)
            self.assertEqual(stat.S_IMODE(file.stat().st_mode), 0o600)

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

    def test_sensitive_broker_fields_are_redacted_recursively(self):
        with tempfile.TemporaryDirectory() as tmp:
            j = DecisionJournal(Path(tmp))
            j.append(
                {
                    "event": "accepted",
                    "order_request": {
                        "account_number": "REAL-ACCOUNT",
                        "symbol": "SPY",
                    },
                    "response": {
                        "rhs_account_number": "12345678",
                        "access_token": "secret-token",
                    },
                }
            )
            record = list(j.iter_today())[0]

        self.assertEqual(record["order_request"]["account_number"], "[redacted]")
        self.assertEqual(record["response"]["rhs_account_number"], "[redacted]")
        self.assertEqual(record["response"]["access_token"], "[redacted]")
        self.assertEqual(record["order_request"]["symbol"], "SPY")
