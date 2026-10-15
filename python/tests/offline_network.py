"""Test-only boundary: a test may reach loopback only on ports it opened itself.

Enforced with an audit hook on every ``socket.connect``/``connect_ex``, below
urllib, http.client, ModelService, the embedding provider and any test double,
so endpoint aliases (``localhost``, ``::1``) and forwarding subclasses cannot
reach the developer's ambient Ollama. Ephemeral fake servers stay reachable:
their listening sockets were bound in this process. A child process inherits
the guard through ``sitecustomize`` (``offline_site`` on PYTHONPATH) and may
also reach the ports its parent test lists in ``ODYSSEUS_TEST_LOOPBACK_PORTS``.
"""
from __future__ import annotations

import ipaddress
import os
import socket
import sys
import weakref

ALLOWED_PORTS_ENV = "ODYSSEUS_TEST_LOOPBACK_PORTS"
_NETWORK_FAMILIES = {socket.AF_INET, socket.AF_INET6}
_bound: weakref.WeakSet[socket.socket] = weakref.WeakSet()
_installed = False


class AmbientRuntimeBlockedError(ConnectionRefusedError):
    """A test tried to connect to a loopback service it did not start."""


def _is_loopback(host: object) -> bool:
    if isinstance(host, bytes):
        host = host.decode("ascii", "replace")
    if host == "localhost":
        return True
    try:
        address = ipaddress.ip_address(str(host).split("%", 1)[0])
    except ValueError:
        return False
    return address.is_loopback or address.is_unspecified  # 0.0.0.0 reaches this host too


def owned_ports() -> set[int]:
    ports = {int(port) for port in os.environ.get(ALLOWED_PORTS_ENV, "").split(",") if port.strip()}
    for sock in list(_bound):
        try:
            ports.add(sock.getsockname()[1])
        except OSError:  # closed
            pass
    return ports


def _audit(event: str, args: tuple) -> None:
    if event == "socket.bind":
        if isinstance(args[0], socket.socket):
            _bound.add(args[0])
        return
    if event != "socket.connect":
        return
    sock, address = args[0], args[1]
    if getattr(sock, "family", None) not in _NETWORK_FAMILIES:
        return
    host, port = address[0], address[1]
    if _is_loopback(host) and port not in owned_ports():
        raise AmbientRuntimeBlockedError(
            f"offline test boundary: loopback port {port} was not opened by this test "
            f"(test: {os.environ.get('PYTEST_CURRENT_TEST', 'unknown')})"
        )


def install() -> None:
    global _installed
    if not _installed:
        sys.addaudithook(_audit)
        _installed = True
