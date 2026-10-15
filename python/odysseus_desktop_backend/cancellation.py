from __future__ import annotations

import threading
import time
from contextlib import contextmanager
from contextvars import ContextVar
from typing import Callable, Iterator


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


def active_cancel_event() -> threading.Event | None:
    """The active job's cancel token, or None outside a cancellation scope."""
    return _CANCEL_EVENT.get()


class TurnDeadlineExceeded(Exception):
    """The interactive turn's absolute deadline has passed; start no more work.

    Like JobCancelledError, deliberately not a RuntimeError/ValueError.
    """


class TurnDeadline:
    """An absolute monotonic deadline. Progress or stage changes never move it."""

    def __init__(self, seconds: float, clock: Callable[[], float] = time.monotonic):
        self.clock = clock
        self.expires_at = clock() + seconds

    def remaining(self) -> float:
        return self.expires_at - self.clock()

    def expired(self) -> bool:
        return self.remaining() <= 0

    def check(self) -> None:
        if self.expired():
            raise TurnDeadlineExceeded("interactive turn deadline reached")

    def allow(self, stage_seconds: float) -> float:
        """The lesser of a stage allowance and what remains; raises when nothing remains."""
        self.check()
        return min(float(stage_seconds), self.remaining())

    def budget(self, seconds: float) -> "TurnDeadline":
        """A shared sub-budget starting now that never outlives this deadline."""
        sub = TurnDeadline(0, self.clock)
        sub.expires_at = min(self.clock() + seconds, self.expires_at)
        return sub


_TURN_DEADLINE: ContextVar[TurnDeadline | None] = ContextVar("odysseus_turn_deadline", default=None)


@contextmanager
def deadline_scope(deadline: TurnDeadline | None) -> Iterator[TurnDeadline | None]:
    token = _TURN_DEADLINE.set(deadline)
    try:
        yield deadline
    finally:
        _TURN_DEADLINE.reset(token)


def active_deadline() -> TurnDeadline | None:
    """The interactive turn's overall deadline, or None outside a chat turn."""
    return _TURN_DEADLINE.get()


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
