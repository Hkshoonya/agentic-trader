"""Broker keys load locally and never show whole anywhere."""

from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path

from agentic_trading.venues.secrets import Credentials, CredentialsError, load_credentials, redact

PAPER_KEY = "PKTESTKEY0000000WXYZ"
PAPER_SECRET = "papersecret-DO-NOT-LEAK-1234567890"
PEM_BODY = "MHcCAQEEIFAKEFAKEFAKEFAKEFAKEFAKEFAKEFAKEFAKEFAKEoAoGCCqGSM49"
PEM = f"-----BEGIN EC PRIVATE KEY-----\n{PEM_BODY}\n-----END EC PRIVATE KEY-----\n"


def _write(tmp: Path, text: str, mode: int = 0o600) -> Path:
    path = tmp / "secrets.toml"
    path.write_text(text, encoding="utf-8")
    os.chmod(path, mode)
    return path


class LoadTests(unittest.TestCase):
    def test_keys_load_from_the_file(self) -> None:
        with tempfile.TemporaryDirectory() as name:
            path = _write(Path(name), f'[alpaca_paper]\nkey = "{PAPER_KEY}"\nsecret = "{PAPER_SECRET}"\n')
            creds = load_credentials(path, env={})
        self.assertEqual(creds["alpaca_paper"].key, PAPER_KEY)
        self.assertEqual(set(creds), {"alpaca_paper"})

    def test_whitespace_is_stripped_and_escaped_pem_newlines_restored(self) -> None:
        escaped = PEM.replace("\n", "\\n")  # what pasting a PEM onto one TOML line gives
        with tempfile.TemporaryDirectory() as name:
            # A TOML *literal* string ('...') keeps "\n" as two characters,
            # exactly what a pasted one-line key gives; "..." would decode it.
            path = _write(Path(name), f"[coinbase]\nkey = '  organizations/o/apiKeys/k  '\nsecret = '{escaped}'\n")
            creds = load_credentials(path, env={})
        self.assertEqual(creds["coinbase"].key, "organizations/o/apiKeys/k")
        self.assertEqual(creds["coinbase"].secret, PEM.strip() + "\n")

    def test_the_environment_fills_what_the_file_lacks(self) -> None:
        env = {"ALPACA_LIVE_KEY": "AKLIVEKEY00000001234", "ALPACA_LIVE_SECRET": "livesecret-xyz-0000"}
        with tempfile.TemporaryDirectory() as name:
            creds = load_credentials(Path(name) / "absent.toml", env=env)
        self.assertEqual(creds["alpaca_live"].key, "AKLIVEKEY00000001234")

    def test_the_file_wins_over_the_environment(self) -> None:
        env = {"ALPACA_PAPER_KEY": "PKFROMENV00000000000", "ALPACA_PAPER_SECRET": "envsecret-0000000"}
        with tempfile.TemporaryDirectory() as name:
            path = _write(Path(name), f'[alpaca_paper]\nkey = "{PAPER_KEY}"\nsecret = "{PAPER_SECRET}"\n')
            creds = load_credentials(path, env=env)
        self.assertEqual(creds["alpaca_paper"].key, PAPER_KEY)

    def test_a_file_others_can_read_is_refused_without_echoing_it(self) -> None:
        with tempfile.TemporaryDirectory() as name:
            path = _write(Path(name), f'[alpaca_paper]\nkey = "{PAPER_KEY}"\nsecret = "{PAPER_SECRET}"\n', 0o644)
            with self.assertRaises(CredentialsError) as caught:
                load_credentials(path, env={})
        self.assertIn("chmod 600", str(caught.exception))
        self.assertNotIn(PAPER_SECRET, str(caught.exception))

    def test_broken_toml_is_reported_without_its_contents(self) -> None:
        with tempfile.TemporaryDirectory() as name:
            path = _write(Path(name), f'[alpaca_paper]\nkey = "{PAPER_KEY}\nsecret = {PAPER_SECRET}\n')
            with self.assertRaises(CredentialsError) as caught:
                load_credentials(path, env={})
        self.assertNotIn(PAPER_SECRET, str(caught.exception))
        self.assertNotIn(PAPER_KEY, str(caught.exception))

    def test_a_half_filled_table_is_ignored(self) -> None:
        with tempfile.TemporaryDirectory() as name:
            path = _write(Path(name), f'[alpaca_paper]\nkey = "{PAPER_KEY}"\n')
            self.assertEqual(load_credentials(path, env={}), {})


class ShowingTests(unittest.TestCase):
    def test_repr_and_str_show_only_the_key_tail(self) -> None:
        cred = Credentials("alpaca_paper", PAPER_KEY, PAPER_SECRET)
        for text in (repr(cred), str(cred), f"{cred}"):
            self.assertNotIn(PAPER_SECRET, text)
            self.assertNotIn(PAPER_KEY, text)
            self.assertIn("WXYZ", text)
        self.assertEqual(cred.hint, "…WXYZ")

    def test_redact_scrubs_keys_secrets_and_pem_lines(self) -> None:
        creds = [Credentials("alpaca_paper", PAPER_KEY, PAPER_SECRET), Credentials("coinbase", "organizations/o/apiKeys/k", PEM)]
        text = f"401 for {PAPER_KEY}:{PAPER_SECRET} while signing with {PEM_BODY}"
        clean = redact(text, creds)
        for leaked in (PAPER_KEY, PAPER_SECRET, PEM_BODY):
            self.assertNotIn(leaked, clean)
        self.assertIn("401", clean)
