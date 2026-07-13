from __future__ import annotations

import threading
from contextlib import contextmanager
from contextvars import ContextVar
from typing import Iterator


class JobCancelledError(Exception):
    """Raised at a safe cancellation point when the active job was cancelled.

    Deliberately not a subclass of RuntimeError/ValueError so broad handlers
    for expected operational failures never swallow a cancellation.
    """


_CANCEL_EVENT: ContextVar[threading.Event | None] = ContextVar(
    "odysseus_cancel_event", default=None
)


@contextmanager
def cancellation_scope(event: threading.Event) -> Iterator[threading.Event]:
    """Make `event` the active cancellation token for this thread/context."""
    token = _CANCEL_EVENT.set(event)
    try:
        yield event
    finally:
        _CANCEL_EVENT.reset(token)


def check_cancelled() -> None:
    """Raise JobCancelledError if the active job requested cancellation.

    Outside a cancellation scope this is a no-op, so library code can call it
    unconditionally at safe points. Never call this inside an open database
    write transaction.
    """
    event = _CANCEL_EVENT.get()
    if event is not None and event.is_set():
        raise JobCancelledError("job cancelled")


def cancellation_requested() -> bool:
    event = _CANCEL_EVENT.get()
    return event is not None and event.is_set()


@contextmanager
def shield() -> Iterator[None]:
    """Suppress cancellation checks for a non-interruptible commit sequence.

    Used around multi-statement commit sequences (e.g. OCR page replacement +
    reindex) where stopping partway would corrupt an already-committed
    document. A cancel requested while shielded resolves as completed —
    cancel never un-commits work (semantics contract §A).
    """
    token = _CANCEL_EVENT.set(None)
    try:
        yield
    finally:
        _CANCEL_EVENT.reset(token)
