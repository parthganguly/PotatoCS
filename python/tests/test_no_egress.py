from __future__ import annotations

import socket

import pytest

from conftest import EgressBlockedError


def test_blocks_non_loopback_connect() -> None:
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        with pytest.raises(EgressBlockedError):
            sock.connect(("8.8.8.8", 80))
    finally:
        sock.close()


def test_blocks_non_loopback_create_connection() -> None:
    with pytest.raises(EgressBlockedError):
        socket.create_connection(("example.com", 80), timeout=1)


def test_allows_loopback_connect() -> None:
    server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    server.bind(("127.0.0.1", 0))
    server.listen(1)
    port = server.getsockname()[1]

    client = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        client.connect(("127.0.0.1", port))
    finally:
        client.close()
        server.close()


def test_allows_localhost_hostname_via_create_connection() -> None:
    server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    server.bind(("127.0.0.1", 0))
    server.listen(1)
    port = server.getsockname()[1]

    try:
        conn = socket.create_connection(("localhost", port), timeout=1)
        conn.close()
    finally:
        server.close()


def test_guard_active_without_opt_in(request: pytest.FixtureRequest) -> None:
    assert request.node.get_closest_marker("allow_egress") is None
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        with pytest.raises(EgressBlockedError):
            sock.connect(("93.184.216.34", 80))
    finally:
        sock.close()


def test_no_test_uses_allow_egress_marker(request: pytest.FixtureRequest) -> None:
    marked = [
        item.nodeid
        for item in request.session.items
        if item.get_closest_marker("allow_egress") is not None
    ]
    assert marked == [], f"allow_egress marker used by: {marked}"
