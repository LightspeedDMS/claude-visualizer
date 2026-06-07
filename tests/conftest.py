"""pytest configuration and shared fixtures for claude-visualizer test suite.

Network guard
-------------
The autouse ``_block_live_network`` fixture patches ``socket.socket.connect``
(and ``connect_ex``) so that any test attempting a non-loopback network
connection fails LOUDLY with a ``RuntimeError`` instead of silently hitting a
live host (e.g. the Proxmox cluster at 192.168.68.15).

Allowed connections:
  - Loopback: 127.0.0.1, ::1, "localhost" (AF_INET / AF_INET6)
  - Unix-domain sockets (AF_UNIX) — used by asyncio internals, Textual, etc.

Blocked:
  - Any AF_INET / AF_INET6 address that is NOT loopback → RuntimeError

This guard means a green test run is proof that NOTHING touched a live host.

The fixture is function-scoped because ``monkeypatch`` is function-scoped;
each test gets a fresh patch that is automatically restored on teardown.
"""

from __future__ import annotations

import socket
from typing import Any

import pytest

# ---------------------------------------------------------------------------
# Loopback allow-list
# ---------------------------------------------------------------------------

_LOOPBACK_HOSTS = frozenset({"127.0.0.1", "::1", "localhost"})


def _is_loopback(addr: Any) -> bool:
    """Return True if ``addr`` is a loopback (safe) destination.

    ``addr`` is the argument passed to ``socket.connect``; for AF_INET /
    AF_INET6 it is a ``(host, port)`` tuple (or longer for IPv6).
    """
    if isinstance(addr, (tuple, list)) and len(addr) >= 2:
        host = addr[0]
        if isinstance(host, str):
            return host in _LOOPBACK_HOSTS or host.startswith("127.")
    return False


# ---------------------------------------------------------------------------
# Network guard fixture
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True, scope="function")
def _block_live_network(monkeypatch: pytest.MonkeyPatch) -> None:
    """Block all outbound non-loopback network connections for every test.

    Uses ``monkeypatch`` (function scope) so the original ``connect`` is
    automatically restored after each test — no global state leakage.

    Allowed: AF_UNIX (asyncio/Textual internals), loopback AF_INET/AF_INET6.
    Blocked: any other AF_INET/AF_INET6 host → RuntimeError (loud failure).
    """
    _orig_connect = socket.socket.connect
    _orig_connect_ex = socket.socket.connect_ex

    def _guarded_connect(self: socket.socket, addr: Any) -> None:  # type: ignore[override]
        if self.family in (socket.AF_INET, socket.AF_INET6):
            if not _is_loopback(addr):
                raise RuntimeError(
                    f"Test attempted a live network connection to {addr!r} — "
                    "tests must not touch live anything. "
                    "Use a loopback address (127.0.0.1 / ::1) or a real local server."
                )
        return _orig_connect(self, addr)

    def _guarded_connect_ex(self: socket.socket, addr: Any) -> int:  # type: ignore[override]
        if self.family in (socket.AF_INET, socket.AF_INET6):
            if not _is_loopback(addr):
                raise RuntimeError(
                    f"Test attempted a live network connection to {addr!r} — "
                    "tests must not touch live anything. "
                    "Use a loopback address (127.0.0.1 / ::1) or a real local server."
                )
        return _orig_connect_ex(self, addr)

    monkeypatch.setattr(socket.socket, "connect", _guarded_connect)
    monkeypatch.setattr(socket.socket, "connect_ex", _guarded_connect_ex)
