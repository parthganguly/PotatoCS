from __future__ import annotations

import os
import socket
import subprocess
import sys
from typing import Iterator

import pytest

from conftest import EgressBlockedError
from offline_network import ALLOWED_PORTS_ENV, AmbientRuntimeBlockedError


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


# --- Offline boundary: loopback services the test did not open are unreachable.

# Stands in for an ambient runtime: a loopback listener owned by another process.
_AMBIENT_LISTENER = """
import socket, sys, threading
server = socket.socket()
server.bind(("127.0.0.1", 0))
server.listen(8)
accepted = [0]
def serve():
    while True:
        conn, _ = server.accept()
        accepted[0] += 1
        conn.close()
threading.Thread(target=serve, daemon=True).start()
print(server.getsockname()[1], flush=True)
sys.stdin.read()
print(accepted[0], flush=True)
"""


class AmbientListener:
    def __init__(self) -> None:
        self.proc = subprocess.Popen(
            [sys.executable, "-c", _AMBIENT_LISTENER],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True,
        )
        assert self.proc.stdout is not None
        self.port = int(self.proc.stdout.readline())

    def accepted(self) -> int:
        out, _ = self.proc.communicate(timeout=10)
        return int(out.strip())


@pytest.fixture
def ambient() -> Iterator[AmbientListener]:
    listener = AmbientListener()
    try:
        yield listener
    finally:
        if listener.proc.poll() is None:
            listener.proc.kill()
            listener.proc.wait(10)


@pytest.mark.parametrize("host", ["127.0.0.1", "localhost", "::1", "0.0.0.0"])
def test_default_ollama_port_is_unreachable_under_every_alias(host: str) -> None:
    # The audit hook raises before the connect syscall, so nothing reaches 11434.
    with pytest.raises((AmbientRuntimeBlockedError, EgressBlockedError)):
        socket.create_connection((host, 11434), timeout=1)


def test_raw_connect_to_ollama_port_is_blocked_before_the_syscall() -> None:
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        with pytest.raises(AmbientRuntimeBlockedError):
            sock.connect(("127.0.0.1", 11434))
        with pytest.raises(AmbientRuntimeBlockedError):
            sock.connect_ex(("127.0.0.1", 11434))
    finally:
        sock.close()


def test_every_inference_path_is_blocked_from_an_ambient_runtime(
    ambient: AmbientListener, monkeypatch: pytest.MonkeyPatch
) -> None:
    from odysseus_desktop_backend.services import model_service
    from odysseus_desktop_backend.services.embedding_service import OllamaEmbeddingProvider

    class ForwardingModel(model_service.ModelService):
        def _post_json(self, *args, **kwargs):  # a double that forwards to the real transport
            return super()._post_json(*args, **kwargs)

    for endpoint in (f"http://127.0.0.1:{ambient.port}", f"http://localhost:{ambient.port}"):
        monkeypatch.setattr(model_service, "OLLAMA_ENDPOINT", endpoint)
        calls = {
            "prepare": lambda: model_service.ModelService(None).prepare("fixture"),
            "forwarding prepare": lambda: ForwardingModel(None).prepare("fixture"),
            "generation": lambda: model_service.ModelService(None).chat("fixture", [{"role": "user", "content": "q"}]),
            "forwarding generation": lambda: ForwardingModel(None).chat_detailed("fixture", [{"role": "user", "content": "q"}]),
            "embedding": lambda: OllamaEmbeddingProvider(endpoint=endpoint).embed(["q"]),
        }
        for name, call in calls.items():
            with pytest.raises(Exception) as caught:
                call()
            chain, exc = [], caught.value
            while exc is not None:
                chain.append(exc)
                exc = exc.__cause__ or exc.__context__
            assert any(isinstance(e, AmbientRuntimeBlockedError) for e in chain), (endpoint, name, chain)
    assert ambient.accepted() == 0


def test_child_processes_inherit_the_boundary(ambient: AmbientListener) -> None:
    probe = f"import socket; socket.create_connection(('127.0.0.1', {ambient.port}), timeout=2)"
    blocked = subprocess.run([sys.executable, "-c", probe], capture_output=True, text=True, timeout=30)
    assert blocked.returncode != 0 and "offline test boundary" in blocked.stderr
    # Positive control: the listener does accept when its port is explicitly granted.
    granted = subprocess.run(
        [sys.executable, "-c", probe], capture_output=True, text=True, timeout=30,
        env={**os.environ, ALLOWED_PORTS_ENV: str(ambient.port)},
    )
    assert granted.returncode == 0, granted.stderr
    assert ambient.accepted() == 1


def test_fake_servers_opened_by_the_test_stay_reachable() -> None:
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
    import threading
    import urllib.request

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            self.send_response(204)
            self.end_headers()

        def log_message(self, *args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        with urllib.request.urlopen(f"http://localhost:{server.server_port}/", timeout=5) as response:
            assert response.status == 204
    finally:
        server.shutdown()
        server.server_close()
