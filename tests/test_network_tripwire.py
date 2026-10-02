"""No test may reach a real broker; the secrets file can never be committed."""

from __future__ import annotations

import socket
import subprocess
import unittest
from pathlib import Path

from tests.network_guard import NetworkBlocked

ROOT = Path(__file__).resolve().parent.parent


class NetworkTripwireTests(unittest.TestCase):
    def test_a_test_cannot_reach_a_remote_host(self) -> None:
        with self.assertRaises(NetworkBlocked):
            socket.create_connection(("203.0.113.9", 443), timeout=1)  # TEST-NET-3

    def test_a_raw_socket_connect_is_blocked_too(self) -> None:
        raw = socket.socket()
        try:
            with self.assertRaises(NetworkBlocked):
                raw.connect(("198.51.100.7", 80))  # TEST-NET-2
            with self.assertRaises(NetworkBlocked):
                raw.connect_ex(("198.51.100.7", 80))
        finally:
            raw.close()

    def test_a_hostname_other_than_localhost_is_blocked(self) -> None:
        with self.assertRaises(NetworkBlocked):
            socket.create_connection(("paper-api.alpaca.markets", 443), timeout=1)

    def test_loopback_still_works(self) -> None:
        server = socket.socket()
        server.bind(("127.0.0.1", 0))
        server.listen(1)
        try:
            client = socket.create_connection(server.getsockname(), timeout=2)
            client.close()
        finally:
            server.close()

    def test_the_secrets_file_is_ignored_by_git(self) -> None:
        done = subprocess.run(
            ["git", "check-ignore", "-q", "config/secrets.toml"], cwd=ROOT
        )
        self.assertEqual(done.returncode, 0)
