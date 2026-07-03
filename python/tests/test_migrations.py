from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from odysseus_desktop_backend.storage import Database, SCHEMA_VERSION


FIXTURE_PATH = Path(__file__).parent / "fixtures" / "v021_schema.sql"


def _schema_snapshot(conn: sqlite3.Connection) -> dict[str, list[tuple[object, ...]]]:
    tables = [
        row[0]
        for row in conn.execute(
            """
            SELECT name
            FROM sqlite_master
            WHERE type = 'table' AND name NOT LIKE 'sqlite_%'
            ORDER BY name
            """
        )
    ]
    return {
        table: [tuple(row) for row in conn.execute(f'PRAGMA table_info("{table}")')]
        for table in tables
    }


def _create_v021_database(profile_dir: Path) -> Path:
    db_path = profile_dir / "app.db"
    profile_dir.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(db_path) as conn:
        conn.executescript(FIXTURE_PATH.read_text(encoding="utf-8"))
        conn.execute(
            """
            INSERT INTO sessions(id, title, model, created_at, updated_at)
            VALUES ('session-1', 'Historical session', 'llama3.2', 1, 1)
            """
        )
        conn.execute(
            """
            INSERT INTO messages(id, session_id, role, content, created_at)
            VALUES ('message-1', 'session-1', 'user', 'Historical message', 2)
            """
        )
        conn.execute(
            """
            INSERT INTO documents(
                id, title, source_path, stored_path, file_name, file_type,
                content_hash, size_bytes, created_at, updated_at
            )
            VALUES (
                'document-1', 'Historical document', 'source.txt', 'stored.txt',
                'source.txt', 'text/plain', 'document-hash', 18, 3, 3
            )
            """
        )
        conn.execute(
            """
            INSERT INTO rag_chunks(
                id, document_id, chunk_index, content, content_hash,
                embedding_model, embedding_hash, created_at, updated_at
            )
            VALUES (
                'chunk-1', 'document-1', 0, 'Historical chunk', 'chunk-hash',
                'nomic-embed-text', 'embedding-hash', 4, 4
            )
            """
        )
    return db_path


def test_fresh_db_stamps_current_schema_version(tmp_path: Path) -> None:
    db = Database(tmp_path)
    try:
        row = db.conn.execute(
            "SELECT value FROM app_meta WHERE key = 'schema_version'"
        ).fetchone()
        assert row["value"] == str(SCHEMA_VERSION)
    finally:
        db.close()


def test_upgrade_from_v021_reaches_fresh_schema_and_preserves_rows(
    tmp_path: Path,
) -> None:
    upgrade_profile = tmp_path / "upgrade"
    fresh_profile = tmp_path / "fresh"
    _create_v021_database(upgrade_profile)

    upgraded = Database(upgrade_profile)
    fresh = Database(fresh_profile)
    try:
        assert _schema_snapshot(upgraded.conn) == _schema_snapshot(fresh.conn)
        assert upgraded.conn.execute(
            "SELECT title FROM sessions WHERE id = 'session-1'"
        ).fetchone()["title"] == "Historical session"
        assert upgraded.conn.execute(
            "SELECT content FROM messages WHERE id = 'message-1'"
        ).fetchone()["content"] == "Historical message"
        assert upgraded.conn.execute(
            "SELECT title FROM documents WHERE id = 'document-1'"
        ).fetchone()["title"] == "Historical document"
        assert upgraded.conn.execute(
            "SELECT content FROM rag_chunks WHERE id = 'chunk-1'"
        ).fetchone()["content"] == "Historical chunk"
        assert upgraded.conn.execute("PRAGMA foreign_key_check").fetchall() == []
        assert upgraded.conn.execute(
            "SELECT value FROM app_meta WHERE key = 'schema_version'"
        ).fetchone()["value"] == str(SCHEMA_VERSION)
    finally:
        upgraded.close()
        fresh.close()


def test_future_version_db_is_refused_without_down_stamp(tmp_path: Path) -> None:
    db_path = tmp_path / "app.db"
    future_version = SCHEMA_VERSION + 1
    with sqlite3.connect(db_path) as conn:
        conn.execute(
            """
            CREATE TABLE app_meta (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL,
                updated_at INTEGER NOT NULL
            )
            """
        )
        conn.execute(
            "INSERT INTO app_meta(key, value, updated_at) VALUES (?, ?, 1)",
            ("schema_version", str(future_version)),
        )

    with pytest.raises(RuntimeError) as exc_info:
        Database(tmp_path)

    message = str(exc_info.value)
    assert str(SCHEMA_VERSION) in message
    assert str(future_version) in message
    assert str(db_path) in message
    with sqlite3.connect(db_path) as conn:
        assert conn.execute(
            "SELECT value FROM app_meta WHERE key = 'schema_version'"
        ).fetchone()[0] == str(future_version)
        assert conn.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table' ORDER BY name"
        ).fetchall() == [("app_meta",)]


def test_init_schema_is_idempotent(tmp_path: Path) -> None:
    db = Database(tmp_path)
    try:
        before = _schema_snapshot(db.conn)
        db.init_schema()
        db.init_schema()
        assert _schema_snapshot(db.conn) == before
        assert db.conn.execute("PRAGMA foreign_key_check").fetchall() == []
        assert db.conn.execute(
            "SELECT value FROM app_meta WHERE key = 'schema_version'"
        ).fetchone()["value"] == str(SCHEMA_VERSION)
    finally:
        db.close()
