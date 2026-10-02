"""Suite-wide guards.

No test may reach a real broker. In a sibling project a unit test quietly sent
requests to a real paper-trading API for months. Here any connect to a
non-loopback address fails the test that tried it. A test marked
``live_venue`` may reach the network only when AGENTIC_LIVE_VENUE_TESTS=1.
"""

from __future__ import annotations

import ipaddress
import os
import socket

import pytest

from tests.network_guard import NetworkBlocked

_REAL_CONNECT = socket.socket.connect
_REAL_CONNECT_EX = socket.socket.connect_ex
_REAL_CREATE_CONNECTION = socket.create_connection


def _is_local(address: object) -> bool:
    if isinstance(address, (str, bytes)):
        return True  # an AF_UNIX path
    host = address[0] if isinstance(address, tuple) and address else ""
    if isinstance(host, bytes):
        host = host.decode()
    if host in ("", "localhost"):
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False  # any other hostname is remote


def _refuse(address: object) -> None:
    raise NetworkBlocked(f"tests may not reach the network: {address!r}")


def pytest_configure(config: pytest.Config) -> None:
    config.addinivalue_line(
        "markers", "live_venue: reaches a real broker; runs only with AGENTIC_LIVE_VENUE_TESTS=1"
    )


@pytest.fixture(autouse=True)
def _no_network(request: pytest.FixtureRequest, monkeypatch: pytest.MonkeyPatch):
    if request.node.get_closest_marker("live_venue"):
        if os.environ.get("AGENTIC_LIVE_VENUE_TESTS") != "1":
            pytest.skip("live venue test: set AGENTIC_LIVE_VENUE_TESTS=1 to run")
        yield
        return

    def connect(self, address):
        if not _is_local(address):
            _refuse(address)
        return _REAL_CONNECT(self, address)

    def connect_ex(self, address):
        if not _is_local(address):
            _refuse(address)
        return _REAL_CONNECT_EX(self, address)

    def create_connection(address, *args, **kwargs):
        if not _is_local(address):
            _refuse(address)
        return _REAL_CREATE_CONNECTION(address, *args, **kwargs)

    monkeypatch.setattr(socket.socket, "connect", connect)
    monkeypatch.setattr(socket.socket, "connect_ex", connect_ex)
    monkeypatch.setattr(socket, "create_connection", create_connection)
    yield
