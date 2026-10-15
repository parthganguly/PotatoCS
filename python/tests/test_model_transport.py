"""ModelService HTTP transport: absolute deadlines and cancellation over real loopback sockets.

A scripted raw-socket server controls every byte: trickling status lines,
headers and bodies, half-open responses and held connections. The socket
timeout alone is an inactivity limit; these tests prove the configured timeout
is a wall-clock deadline and that cancellation leaves nothing able to send.
"""

from __future__ import annotations

import errno
import re
import socket
import socketserver
import threading
import time
from typing import Any, Callable

import pytest

from odysseus_desktop_backend.cancellation import JobCancelledError, cancellation_scope
from odysseus_desktop_backend.services import model_service
from odysseus_desktop_backend.services.model_service import (
    ModelPreparationError,
    ModelPreparationTimeoutError,
    ModelService,
    ModelTimeoutError,
)

STEP = 0.05
LONG_BODY = b'{"ok": true' + b" " * 80 + b"}"  # valid JSON, ~4.5 s at one byte per step


def trickle(conn: socket.socket, data: bytes) -> None:
    for byte in data:
        conn.sendall(bytes([byte]))
        time.sleep(STEP)


class ScriptedServer:
    """Loopback HTTP peer: reads one request per connection, then runs `mode`."""

    def __init__(self, mode: str) -> None:
        self.mode = mode
        self.connections = 0
        self.requests = 0
        self.arrived = threading.Event()
        self.disconnected = threading.Event()
        self.release = threading.Event()
        server = self

        class Handler(socketserver.BaseRequestHandler):
            def handle(self) -> None:
                conn: socket.socket = self.request
                server.connections += 1
                data = b""
                while b"\r\n\r\n" not in data:
                    chunk = conn.recv(4096)
                    if not chunk:
                        return  # connected, but no request was ever sent
                    data += chunk
                head, _, body = data.partition(b"\r\n\r\n")
                length = int(re.search(rb"content-length: *(\d+)", head, re.I).group(1))
                while len(body) < length:
                    body += conn.recv(4096)
                server.requests += 1
                server.arrived.set()
                try:
                    server.respond(conn)
                except OSError:
                    server.disconnected.set()

        self.server = socketserver.ThreadingTCPServer(("127.0.0.1", 0), Handler)
        self.server.daemon_threads = True
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}/api/chat"
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def respond(self, conn: socket.socket) -> None:
        mode = self.mode
        if mode == "ok":
            conn.sendall(b"HTTP/1.1 200 OK\r\nContent-Length: 11\r\n\r\n" + b'{"ok":true}')
        elif mode == "slow_status":
            trickle(conn, b"HTTP/1.1 200 OK\r\n" + b"X-Pad: " + b"a" * 80 + b"\r\n")
            conn.sendall(b"Content-Length: 11\r\n\r\n{\"ok\":true}")
        elif mode == "slow_body":
            conn.sendall(b"HTTP/1.1 200 OK\r\nContent-Length: %d\r\n\r\n" % len(LONG_BODY))
            trickle(conn, LONG_BODY)
        elif mode == "slow_error_body":
            conn.sendall(b"HTTP/1.1 500 Internal Server Error\r\nContent-Length: %d\r\n\r\n" % len(LONG_BODY))
            trickle(conn, LONG_BODY)
        else:
            if mode == "partial_headers":
                conn.sendall(b"HTTP/1.1 200")
            elif mode in {"incomplete_body", "incomplete_error_body"}:
                status = b"400 Bad Request" if mode == "incomplete_error_body" else b"200 OK"
                conn.sendall(b"HTTP/1.1 " + status + b"\r\nContent-Length: 1000\r\n\r\n{")
            self.hold(conn)  # "blocked_headers" sends nothing at all

    def hold(self, conn: socket.socket) -> None:
        conn.settimeout(0.02)
        while not self.release.is_set():
            try:
                if conn.recv(1, socket.MSG_PEEK) == b"":
                    break
            except socket.timeout:
                continue
            except OSError:
                break
        self.disconnected.set()

    def close(self) -> None:
        self.release.set()
        self.server.shutdown()
        self.server.server_close()


@pytest.fixture()
def serve():
    servers: list[ScriptedServer] = []

    def start(mode: str) -> ScriptedServer:
        servers.append(ScriptedServer(mode))
        return servers[-1]

    yield start
    for server in servers:
        server.close()


def transport_threads() -> list[threading.Thread]:
    return [t for t in threading.enumerate() if t.name.startswith("odysseus-model")]


def timed(call: Callable[[], Any]) -> tuple[BaseException | None, float]:
    started = time.monotonic()
    try:
        call()
    except BaseException as exc:  # noqa: BLE001 - the test inspects it
        return exc, time.monotonic() - started
    return None, time.monotonic() - started


class Cancellable:
    """Runs a ModelService call inside a job cancellation scope on its own thread."""

    def __init__(self, call: Callable[[], Any]) -> None:
        self.cancel = threading.Event()
        self.error: BaseException | None = None
        self.finished_at = 0.0

        def run() -> None:
            with cancellation_scope(self.cancel):
                try:
                    call()
                except BaseException as exc:  # noqa: BLE001
                    self.error = exc
            self.finished_at = time.monotonic()

        self.thread = threading.Thread(target=run)
        self.thread.start()


@pytest.mark.parametrize("mode", ["slow_status", "slow_body", "slow_error_body"])
@pytest.mark.parametrize("scoped", [False, True], ids=["chat.send", "job"])
def test_trickling_response_cannot_outlive_the_deadline(serve, mode: str, scoped: bool) -> None:
    server = serve(mode)

    def call() -> None:
        if scoped:
            with cancellation_scope(threading.Event()):
                ModelService(None)._post_json(server.url, {"messages": []}, timeout=0.4)
        else:
            ModelService(None)._post_json(server.url, {"messages": []}, timeout=0.4)

    error, elapsed = timed(call)
    assert isinstance(error, ModelTimeoutError), error
    assert elapsed < 0.4 + 0.3, f"returned after {elapsed:.2f}s; each byte arrived within the inactivity limit"
    assert server.disconnected.wait(2), "the server must see the transport closed at the deadline"
    assert not transport_threads()


def test_connection_that_never_completes_hits_the_deadline(serve, monkeypatch: pytest.MonkeyPatch) -> None:
    server = serve("ok")

    def unanswered(r: Any, w: Any, x: Any, timeout: float) -> Any:  # a SYN nobody answers
        time.sleep(timeout)
        return [], [], []

    monkeypatch.setattr(socket.socket, "connect_ex", lambda sock, address: errno.EINPROGRESS)
    monkeypatch.setattr(model_service.select, "select", unanswered)
    error, elapsed = timed(lambda: ModelService(None)._post_json(server.url, {"messages": []}, timeout=0.3))
    monkeypatch.undo()
    assert isinstance(error, ModelTimeoutError), error
    assert elapsed < 0.3 + 0.3
    time.sleep(0.2)
    assert server.requests == 0


def test_stage_errors_stay_distinguishable_under_the_deadline(serve) -> None:
    server = serve("slow_body")
    endpoint = server.url.rsplit("/api/chat", 1)[0]
    original = model_service.OLLAMA_ENDPOINT
    model_service.OLLAMA_ENDPOINT = endpoint
    try:
        prepare_error, _ = timed(lambda: ModelService(None).prepare("fixture", timeout=0.3))
        generate_error, _ = timed(
            lambda: ModelService(None).chat_detailed("fixture", [{"role": "user", "content": "q"}], timeout=0.3)
        )
    finally:
        model_service.OLLAMA_ENDPOINT = original
    assert isinstance(prepare_error, ModelPreparationTimeoutError)
    assert isinstance(generate_error, ModelTimeoutError) and not isinstance(generate_error, ModelPreparationError)


@pytest.mark.parametrize(
    "mode", ["blocked_headers", "partial_headers", "incomplete_body", "incomplete_error_body"]
)
def test_cancel_stops_a_half_open_response(serve, mode: str) -> None:
    server = serve(mode)
    job = Cancellable(lambda: ModelService(None)._post_json(server.url, {"messages": []}, timeout=30))
    assert server.arrived.wait(5)
    time.sleep(0.1)
    started = time.monotonic()
    job.cancel.set()
    job.thread.join(3)
    assert not job.thread.is_alive()
    assert isinstance(job.error, JobCancelledError), job.error
    assert job.finished_at - started < 1.0
    assert server.disconnected.wait(2)
    assert not transport_threads()


def test_cancel_during_connection_never_sends_a_later_post(serve, monkeypatch: pytest.MonkeyPatch) -> None:
    """Sol's delayed-connection race: the connect completes only after cancel was requested."""
    server = serve("ok")
    port = server.server.server_address[1]
    connecting = threading.Event()
    release = threading.Event()
    real_connect_ex = socket.socket.connect_ex

    def held(sock: socket.socket, address: Any) -> int:
        status = real_connect_ex(sock, address)
        if address[1] == port:
            connecting.set()
            assert release.wait(10)
        return status

    monkeypatch.setattr(socket.socket, "connect_ex", held)
    job = Cancellable(lambda: ModelService(None)._post_json(server.url, {"messages": []}, timeout=30))
    assert connecting.wait(5)
    job.cancel.set()
    time.sleep(0.3)
    assert job.thread.is_alive(), "cancel must not be reported while the request can still send"
    release.set()
    job.thread.join(3)
    assert not job.thread.is_alive()
    assert isinstance(job.error, JobCancelledError), job.error
    time.sleep(0.5)  # a leaked sender would deliver its POST now
    assert server.requests == 0
    assert not transport_threads()


def test_cancel_racing_the_deadline_has_exactly_one_outcome(serve) -> None:
    server = serve("blocked_headers")
    for _ in range(8):
        job = Cancellable(lambda: ModelService(None)._post_json(server.url, {"messages": []}, timeout=0.2))
        timer = threading.Timer(0.2, job.cancel.set)
        timer.start()
        job.thread.join(3)
        timer.join()
        assert not job.thread.is_alive()
        assert isinstance(job.error, (JobCancelledError, ModelTimeoutError)), job.error
        assert not transport_threads()


def test_cancel_requested_before_the_request_sends_nothing(serve) -> None:
    server = serve("ok")
    cancel = threading.Event()
    cancel.set()
    with cancellation_scope(cancel):
        with pytest.raises(JobCancelledError):
            ModelService(None)._post_json(server.url, {"messages": []}, timeout=5)
    time.sleep(0.2)
    assert server.connections == 0


def test_successful_response_is_unchanged(serve) -> None:
    server = serve("ok")
    assert ModelService(None)._post_json(server.url, {"messages": []}, timeout=5) == {"ok": True}
    with cancellation_scope(threading.Event()):
        assert ModelService(None)._post_json(server.url, {"messages": []}, timeout=5) == {"ok": True}
    assert not transport_threads()
