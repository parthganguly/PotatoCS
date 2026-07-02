from __future__ import annotations

import ipaddress
import os
import socket
import sys
from pathlib import Path

import pytest

PYTHON_DIR = Path(__file__).resolve().parents[1]
if str(PYTHON_DIR) not in sys.path:
    sys.path.insert(0, str(PYTHON_DIR))

os.environ["ODYSSEUS_STRICT_TRACE"] = "1"


class EgressBlockedError(RuntimeError):
    """Raised when a test attempts a socket connection to a non-loopback host."""


_LOOPBACK_HOSTS = {"127.0.0.1", "localhost", "::1"}
_NETWORK_FAMILIES = {socket.AF_INET, socket.AF_INET6}


def _is_loopback_host(host: object) -> bool:
    if isinstance(host, bytes):
        host = host.decode("utf-8", "replace")
    if host in _LOOPBACK_HOSTS:
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except (ValueError, TypeError):
        return False


def _host_from_address(address: object) -> object:
    if isinstance(address, tuple) and address:
        return address[0]
    return address


_real_socket_connect = socket.socket.connect
_real_create_connection = socket.create_connection


def _guarded_socket_connect(self, address, *args, **kwargs):
    if getattr(self, "family", None) in _NETWORK_FAMILIES:
        host = _host_from_address(address)
        if not _is_loopback_host(host):
            raise EgressBlockedError(
                f"blocked non-loopback socket connection to {host!r} "
                f"(test: {os.environ.get('PYTEST_CURRENT_TEST', 'unknown')})"
            )
    return _real_socket_connect(self, address, *args, **kwargs)


def _guarded_create_connection(address, *args, **kwargs):
    host = _host_from_address(address)
    if not _is_loopback_host(host):
        raise EgressBlockedError(
            f"blocked non-loopback socket connection to {host!r} "
            f"(test: {os.environ.get('PYTEST_CURRENT_TEST', 'unknown')})"
        )
    return _real_create_connection(address, *args, **kwargs)


def pytest_configure(config: pytest.Config) -> None:
    config.addinivalue_line(
        "markers",
        "allow_egress: opt out of the autouse non-loopback egress guard "
        "(see python/tests/conftest.py)",
    )


@pytest.fixture(autouse=True)
def _block_non_loopback_egress(request: pytest.FixtureRequest, monkeypatch: pytest.MonkeyPatch):
    if request.node.get_closest_marker("allow_egress") is not None:
        return
    monkeypatch.setattr(socket.socket, "connect", _guarded_socket_connect)
    monkeypatch.setattr(socket, "create_connection", _guarded_create_connection)
