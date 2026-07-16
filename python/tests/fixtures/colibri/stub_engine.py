"""Deterministic stub of the Colibri inference engine's stdio protocol.

Used to run the REAL upstream `coli serve` / openai_server.py without the
MinGW-built glm engine or any model weights: upstream's Engine class spawns
whatever COLI_ENGINE points at and speaks this protocol over stdin/stdout.

Protocol (as implemented by upstream openai_server.py at 54cfe563):
  engine -> server:  b"\\x01\\x01READY\\x01\\x01\\n" once at startup, then one
                     b"STAT <ctok> <tps> <hit%> <rss> <ptok> <lenlim>\\n" line
                     (read_engine_turn parses a STAT line after every sentinel)
  server -> engine:  b"SUBMIT <id> <slot> <payload_len> <max_tokens> <temp> <top_p>\\n"
                     + payload bytes + b"\\n"
                     b"CANCEL <id>\\n"
  engine -> server:  b"DATA <id> <size>\\n" + size bytes + b"\\n"
                     b"DONE <id> STAT <ctok> <tps> <hit%> <rss> <ptok> <lenlim>\\n"
                     b"ERROR <id> <message>\\n"  (message CANCELLED acknowledges a cancel)

Behavior knobs (in the prompt payload, so one server run covers all cases):
  "STUB_SLOW"  -> 0.15 s per token and 200 tokens, so a test can cancel mid-flight.
This emits text token-by-token and honors CANCEL between tokens. It never
touches the network or the filesystem.
"""

from __future__ import annotations

import queue
import sys
import threading
import time

READY = b"\x01\x01READY\x01\x01\n"
ANSWER_TOKENS = ["Deep ", "Local ", "stub ", "answer: ", "the ", "notice ", "period ", "is ", "30 ", "days."]
SLOW_TOKEN = "tick "
SLOW_TOKEN_COUNT = 200
SLOW_DELAY_SECONDS = 0.15


def _write(data: bytes) -> None:
    sys.stdout.buffer.write(data)
    sys.stdout.buffer.flush()


def _emit_data(request_id: str, text: str) -> None:
    payload = text.encode("utf-8")
    _write(f"DATA {request_id} {len(payload)}\n".encode() + payload + b"\n")


def _emit_done(request_id: str, completion_tokens: int, prompt_tokens: int, elapsed: float) -> None:
    tps = completion_tokens / elapsed if elapsed > 0 else 99.0
    _write(
        f"DONE {request_id} STAT {completion_tokens} {tps:.4f} 100.0 0.1 {prompt_tokens} 0\n".encode()
    )


def _reader(commands: "queue.Queue[tuple[str, ...]]") -> None:
    stdin = sys.stdin.buffer
    while True:
        header = stdin.readline()
        if not header:
            commands.put(("eof",))
            return
        fields = header.decode("utf-8", "replace").strip().split()
        if not fields:
            continue
        if fields[0] == "SUBMIT" and len(fields) >= 6:
            request_id, _slot, payload_len = fields[1], fields[2], int(fields[3])
            max_tokens = int(fields[4])
            payload = stdin.read(payload_len)
            stdin.read(1)  # trailing newline
            commands.put(("submit", request_id, payload.decode("utf-8", "replace"), str(max_tokens)))
        elif fields[0] == "CANCEL" and len(fields) >= 2:
            commands.put(("cancel", fields[1]))


def main() -> int:
    _write(READY)
    _write(b"STAT 0 0.0 0.0 0.1 0 0\n")
    commands: "queue.Queue[tuple[str, ...]]" = queue.Queue()
    threading.Thread(target=_reader, args=(commands,), daemon=True).start()
    cancelled: set[str] = set()
    while True:
        command = commands.get()
        if command[0] == "eof":
            return 0
        if command[0] == "cancel":
            cancelled.add(command[1])
            continue
        _kind, request_id, prompt, max_tokens_text = command
        max_tokens = int(max_tokens_text)
        slow = "STUB_SLOW" in prompt
        tokens = [SLOW_TOKEN] * SLOW_TOKEN_COUNT if slow else ANSWER_TOKENS
        tokens = tokens[:max_tokens]
        prompt_tokens = max(1, len(prompt.split()))
        started = time.monotonic()
        emitted = 0
        was_cancelled = False
        for token in tokens:
            # Drain pending commands so CANCEL for this request is seen.
            try:
                while True:
                    pending = commands.get_nowait()
                    if pending[0] == "cancel":
                        cancelled.add(pending[1])
                    elif pending[0] == "eof":
                        return 0
                    else:
                        commands.put(pending)  # unlikely: overlapping SUBMIT
                        break
            except queue.Empty:
                pass
            if request_id in cancelled:
                was_cancelled = True
                break
            _emit_data(request_id, token)
            emitted += 1
            if slow:
                time.sleep(SLOW_DELAY_SECONDS)
        if was_cancelled:
            _write(f"ERROR {request_id} CANCELLED\n".encode())
            continue
        _emit_done(request_id, emitted, prompt_tokens, time.monotonic() - started)


if __name__ == "__main__":
    raise SystemExit(main())
