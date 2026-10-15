"""Durable chat turns: one logical user message, explicitly tracked attempts,
at most one committed assistant answer.

Every state change is one SQLite write transaction (BEGIN IMMEDIATE), so the
RPC thread and the job worker, each on its own connection, are serialized by
the database. Answer commit, cancellation and retirement are conditional
updates on the same row, so exactly one of them wins.
"""

from __future__ import annotations

import json
import sqlite3
import uuid
from contextlib import contextmanager
from typing import Any, Iterator

from odysseus_desktop_backend.cancellation import JobCancelledError, cancellation_requested
from odysseus_desktop_backend.storage import Database, utc_ms


ACTIVE_STATES = ("pending", "running")
RETRYABLE_STATES = ("failed", "cancelled", "timed_out", "interrupted")
MAX_OPERATION_KEY_LENGTH = 128


class AttemptRetired(Exception):
    """This attempt may no longer commit: it was retired, superseded or its turn is gone."""


class ChatTurnStore:
    def __init__(self, db: Database):
        self.db = db

    @contextmanager
    def _write(self) -> Iterator[sqlite3.Connection]:
        conn = self.db.conn
        if conn.in_transaction:
            # ponytail: callers commit their own writes; this only closes an implicit read.
            conn.commit()
        conn.execute("BEGIN IMMEDIATE")
        try:
            yield conn
        except BaseException:
            conn.rollback()
            raise
        conn.commit()

    # ------------------------------------------------------------ submission

    def accept(
        self,
        *,
        session_id: str,
        content: str,
        attachment_document_ids: list[str],
        request: dict[str, Any],
        operation_key: str,
    ) -> tuple[dict[str, Any], bool]:
        """Persist the logical user message, its sources, the turn and attempt 1, atomically.

        Returns (turn status, created). A known operation key returns the existing
        operation and creates nothing.
        """
        key = clean_operation_key(operation_key)
        with self._write() as conn:
            existing = conn.execute("SELECT message_id FROM chat_attempts WHERE operation_key = ?", (key,)).fetchone()
            if existing is not None:
                return self._status(conn, existing["message_id"]), False
            for document_id in attachment_document_ids:
                _require_usable_document(conn, document_id)
            now = utc_ms()
            message_id = str(uuid.uuid4())
            attempt_id = str(uuid.uuid4())
            conn.execute(
                "INSERT INTO messages(id, session_id, role, content, metadata_json, created_at) VALUES (?, ?, 'user', ?, '{}', ?)",
                (message_id, session_id, content, now),
            )
            for order, document_id in enumerate(attachment_document_ids):
                scope = conn.execute("SELECT scope FROM documents WHERE id = ?", (document_id,)).fetchone()["scope"]
                conn.execute(
                    """
                    INSERT OR IGNORE INTO message_documents(message_id, document_id, display_order, scope_at_link, created_at)
                    VALUES (?, ?, ?, ?, ?)
                    """,
                    (message_id, document_id, order, str(scope or "library"), now),
                )
                conn.execute(
                    """
                    INSERT INTO conversation_attachments(session_id, backend_kind, source_id, added_message_id, created_at, removed_at)
                    VALUES (?, 'document', ?, ?, ?, NULL)
                    ON CONFLICT(session_id, backend_kind, source_id) DO UPDATE SET
                        added_message_id = excluded.added_message_id, removed_at = NULL, created_at = excluded.created_at
                    """,
                    (session_id, document_id, message_id, now),
                )
            conn.execute(
                "UPDATE sessions SET updated_at = ?, last_message_at = ? WHERE id = ?",
                (now, now, session_id),
            )
            conn.execute(
                """
                INSERT INTO chat_turns(message_id, session_id, request_json, current_attempt_id, created_at)
                VALUES (?, ?, ?, ?, ?)
                """,
                (message_id, session_id, json.dumps(request, sort_keys=True), attempt_id, now),
            )
            self._insert_attempt(conn, attempt_id, message_id, session_id, 1, key, now)
            return self._status(conn, message_id), True

    def retry(self, message_id: str, operation_key: str) -> tuple[dict[str, Any], bool]:
        """Create the next attempt for an unanswered latest turn; no new user message."""
        key = clean_operation_key(operation_key)
        with self._write() as conn:
            existing = conn.execute("SELECT message_id FROM chat_attempts WHERE operation_key = ?", (key,)).fetchone()
            if existing is not None:
                return self._status(conn, existing["message_id"]), False
            turn = conn.execute("SELECT * FROM chat_turns WHERE message_id = ?", (message_id,)).fetchone()
            if turn is None:
                raise KeyError(f"chat turn not found: {message_id}")
            status = self._status(conn, message_id)
            if status["answered"]:
                raise ValueError("this question already has an answer")
            if status["latest_state"] in ACTIVE_STATES:
                raise ValueError("an attempt for this question is still in progress")
            if not status["is_latest_turn"]:
                raise ValueError("only the latest question can be retried; copy it to a new message instead")
            if status["latest_state"] not in RETRYABLE_STATES:
                raise ValueError("this question cannot be retried")
            # The original sources, never substitutes: a missing one is an explicit error.
            request = json.loads(turn["request_json"] or "{}")
            linked = [
                row["document_id"]
                for row in conn.execute(
                    "SELECT document_id FROM message_documents WHERE message_id = ? ORDER BY display_order", (message_id,)
                ).fetchall()
            ]
            for document_id in linked + list(request.get("document_ids") or []):
                _require_usable_document(conn, document_id)
            attempt_id = str(uuid.uuid4())
            now = utc_ms()
            self._insert_attempt(conn, attempt_id, message_id, turn["session_id"], status["attempt_count"] + 1, key, now)
            conn.execute("UPDATE chat_turns SET current_attempt_id = ? WHERE message_id = ?", (attempt_id, message_id))
            return self._status(conn, message_id), True

    def _insert_attempt(
        self, conn: sqlite3.Connection, attempt_id: str, message_id: str, session_id: str, number: int, key: str, now: int
    ) -> None:
        try:
            conn.execute(
                """
                INSERT INTO chat_attempts(id, message_id, session_id, attempt_number, operation_key, state, created_at)
                VALUES (?, ?, ?, ?, ?, 'pending', ?)
                """,
                (attempt_id, message_id, session_id, number, key, now),
            )
        except sqlite3.IntegrityError as exc:
            # idx_chat_attempts_one_active_per_session: someone else holds the conversation.
            raise ValueError("another answer is still in progress in this conversation") from exc

    def request(self, message_id: str) -> tuple[str, str, list[str], dict[str, Any]]:
        """(session_id, original text, original attachment ids, original request options)."""
        conn = self.db.conn
        row = conn.execute(
            """
            SELECT t.session_id, t.request_json, m.content FROM chat_turns t
            JOIN messages m ON m.id = t.message_id WHERE t.message_id = ?
            """,
            (message_id,),
        ).fetchone()
        if row is None:
            raise KeyError(f"chat turn not found: {message_id}")
        attachments = [
            item["document_id"]
            for item in conn.execute(
                "SELECT document_id FROM message_documents WHERE message_id = ? ORDER BY display_order", (message_id,)
            ).fetchall()
        ]
        return row["session_id"], row["content"], attachments, json.loads(row["request_json"] or "{}")

    # ------------------------------------------------------------- execution

    def start(self, attempt_id: str) -> None:
        """pending -> running, unless cancelled or retired first."""
        with self._write() as conn:
            moved = conn.execute(
                "UPDATE chat_attempts SET state = 'running', started_at = ? WHERE id = ? AND state = 'pending' AND cancel_requested = 0",
                (utc_ms(), attempt_id),
            ).rowcount
            if moved:
                return
            row = conn.execute("SELECT state, cancel_requested FROM chat_attempts WHERE id = ?", (attempt_id,)).fetchone()
        if row is not None and (row["state"] == "cancelled" or row["cancel_requested"]):
            raise JobCancelledError("chat attempt cancelled before it started")
        raise AttemptRetired(f"chat attempt cannot start: {attempt_id}")

    def request_cancel(self, attempt_id: str) -> None:
        """A queued attempt is cancelled now; a running one is marked and settles in its worker."""
        with self._write() as conn:
            now = utc_ms()
            conn.execute(
                """
                UPDATE chat_attempts SET state = 'cancelled', outcome_code = 'cancelled_by_user', finished_at = ?
                WHERE id = ? AND state = 'pending'
                """,
                (now, attempt_id),
            )
            conn.execute(
                "UPDATE chat_attempts SET cancel_requested = 1 WHERE id = ? AND state = 'running'",
                (attempt_id,),
            )

    def commit_answer(
        self,
        attempt_id: str,
        *,
        content: str,
        metadata: dict[str, Any],
        title: str,
    ) -> dict[str, Any]:
        """Atomically save the answer if, and only if, this attempt may still commit.

        Raises JobCancelledError when a cancel was recorded first and AttemptRetired
        when the attempt was retired or superseded; in both cases nothing is saved.
        """
        with self._write() as conn:
            row = conn.execute(
                """
                SELECT a.state, a.cancel_requested, a.message_id, a.session_id,
                       t.current_attempt_id, t.answer_message_id
                FROM chat_attempts a JOIN chat_turns t ON t.message_id = a.message_id
                WHERE a.id = ?
                """,
                (attempt_id,),
            ).fetchone()
            # Inside the write lock: a cancel seen here wins and nothing is saved; one
            # recorded after this transaction finds the attempt completed.
            if row is not None and (row["cancel_requested"] or cancellation_requested()):
                raise JobCancelledError("chat attempt cancelled before its answer was saved")
            if (
                row is None
                or row["state"] != "running"
                or row["current_attempt_id"] != attempt_id
                or row["answer_message_id"] is not None
            ):
                raise AttemptRetired(f"chat attempt may not commit: {attempt_id}")
            now = utc_ms()
            answer_id = str(uuid.uuid4())
            encoded = json.dumps(metadata, ensure_ascii=False, separators=(",", ":"))
            conn.execute(
                "INSERT INTO messages(id, session_id, role, content, metadata_json, created_at) VALUES (?, ?, 'assistant', ?, ?, ?)",
                (answer_id, row["session_id"], content, encoded, now),
            )
            conn.execute(
                "UPDATE chat_attempts SET state = 'completed', answer_message_id = ?, finished_at = ? WHERE id = ?",
                (answer_id, now, attempt_id),
            )
            conn.execute("UPDATE chat_turns SET answer_message_id = ? WHERE message_id = ?", (answer_id, row["message_id"]))
            conn.execute(
                """
                UPDATE sessions SET updated_at = ?, last_message_at = ?,
                    title = CASE WHEN title = 'New chat' THEN ? ELSE title END
                WHERE id = ?
                """,
                (now, now, title, row["session_id"]),
            )
        return {
            "id": answer_id,
            "session_id": row["session_id"],
            "role": "assistant",
            "content": content,
            "metadata": metadata,
            "created_at": now,
        }

    def finish(self, attempt_id: str, state: str, outcome_code: str) -> bool:
        """Settle an attempt that did not commit. A settled attempt is never changed."""
        with self._write() as conn:
            return bool(
                conn.execute(
                    """
                    UPDATE chat_attempts SET state = ?, outcome_code = ?, finished_at = ?
                    WHERE id = ? AND state IN ('pending', 'running')
                    """,
                    (state, outcome_code, utc_ms(), attempt_id),
                ).rowcount
            )

    def state(self, attempt_id: str) -> str:
        row = self.db.conn.execute("SELECT state FROM chat_attempts WHERE id = ?", (attempt_id,)).fetchone()
        return str(row["state"]) if row else ""

    def reconcile_interrupted(self) -> int:
        """At startup no worker of an earlier process can still own an attempt; retire them.

        Retiring is the same conditional update a commit races against, so even a
        surviving earlier process could not save an answer afterwards.
        """
        with self._write() as conn:
            return conn.execute(
                """
                UPDATE chat_attempts SET state = 'interrupted', outcome_code = 'backend_restarted', finished_at = ?
                WHERE state IN ('pending', 'running')
                """,
                (utc_ms(),),
            ).rowcount

    # --------------------------------------------------------------- reading

    def status(self, *, message_id: str = "", operation_key: str = "") -> dict[str, Any] | None:
        conn = self.db.conn
        if operation_key:
            row = conn.execute(
                "SELECT message_id FROM chat_attempts WHERE operation_key = ?", (clean_operation_key(operation_key),)
            ).fetchone()
            if row is None:
                return None
            message_id = row["message_id"]
        if conn.execute("SELECT 1 FROM chat_turns WHERE message_id = ?", (message_id,)).fetchone() is None:
            return None
        return self._status(conn, message_id)

    def annotate(self, session_id: str, messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """Add each tracked user message's turn status; legacy messages stay untouched."""
        rows = self.db.conn.execute("SELECT message_id FROM chat_turns WHERE session_id = ?", (session_id,)).fetchall()
        tracked = {row["message_id"] for row in rows}
        for message in messages:
            if message.get("id") in tracked:
                message["turn"] = self._status(self.db.conn, str(message["id"]))
        return messages

    def unanswered_message_ids(self, session_id: str) -> set[str]:
        """Tracked user messages without a committed answer; never part of model history."""
        rows = self.db.conn.execute(
            "SELECT message_id FROM chat_turns WHERE session_id = ? AND answer_message_id IS NULL", (session_id,)
        ).fetchall()
        return {row["message_id"] for row in rows}

    def _status(self, conn: sqlite3.Connection, message_id: str) -> dict[str, Any]:
        turn = conn.execute("SELECT * FROM chat_turns WHERE message_id = ?", (message_id,)).fetchone()
        attempts = [
            {
                "attempt_id": row["id"],
                "number": int(row["attempt_number"]),
                "operation_key": row["operation_key"],
                "state": row["state"],
                "outcome_code": row["outcome_code"],
                "created_at": row["created_at"],
                "started_at": row["started_at"],
                "finished_at": row["finished_at"],
            }
            for row in conn.execute(
                "SELECT * FROM chat_attempts WHERE message_id = ? ORDER BY attempt_number", (message_id,)
            ).fetchall()
        ]
        latest = attempts[-1]
        latest_user = conn.execute(
            "SELECT id FROM messages WHERE session_id = ? AND role = 'user' ORDER BY created_at DESC, rowid DESC LIMIT 1",
            (turn["session_id"],),
        ).fetchone()
        session_busy = conn.execute(
            "SELECT 1 FROM chat_attempts WHERE session_id = ? AND state IN ('pending', 'running')",
            (turn["session_id"],),
        ).fetchone()
        answered = turn["answer_message_id"] is not None
        is_latest = latest_user is not None and latest_user["id"] == message_id
        return {
            "message_id": message_id,
            "session_id": turn["session_id"],
            "answered": answered,
            "answer_message_id": turn["answer_message_id"],
            "current_attempt_id": turn["current_attempt_id"],
            "latest_state": latest["state"],
            "latest_outcome_code": latest["outcome_code"],
            "attempt_count": len(attempts),
            "attempts": attempts,
            "is_latest_turn": is_latest,
            "can_retry": (
                not answered and is_latest and session_busy is None and latest["state"] in RETRYABLE_STATES
            ),
            "can_copy": not answered and not is_latest and latest["state"] not in ACTIVE_STATES,
        }


def clean_operation_key(value: Any) -> str:
    key = str(value or "").strip()
    if not key or len(key) > MAX_OPERATION_KEY_LENGTH:
        raise ValueError("operation_key must be a non-empty string of at most 128 characters")
    return key


def _require_usable_document(conn: sqlite3.Connection, document_id: str) -> None:
    row = conn.execute("SELECT is_deleted FROM documents WHERE id = ?", (document_id,)).fetchone()
    if row is None or row["is_deleted"]:
        raise ValueError(f"an original source is no longer available: {document_id}")
