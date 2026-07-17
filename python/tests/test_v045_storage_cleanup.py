"""Issue #17 — profile storage visibility, safe deletion, and cleanup
(V04_STORAGE_CLEANUP_DESIGN.md).

Scratch profiles and synthetic fixtures only; the embedding provider is the
deterministic lexical fallback, so no Ollama is involved. Windows junction
fixtures are created with `mklink /J` (no elevation needed); if junction
creation is unavailable the guard is unit-tested directly.
"""

from __future__ import annotations

import os
import sqlite3
import subprocess
import sys
import uuid
from pathlib import Path

import pytest

from odysseus_desktop_backend.pathsafety import is_link
from odysseus_desktop_backend.services.artifact_service import ArtifactService
from odysseus_desktop_backend.services.document_service import DocumentService
from odysseus_desktop_backend.services.embedding_service import (
    EmbeddingService,
    LocalHashEmbeddingProvider,
)
from odysseus_desktop_backend.services.job_service import JobService
from odysseus_desktop_backend.services.rag_service import RAGService
from odysseus_desktop_backend.services.source_service import SourceService
from odysseus_desktop_backend.services.storage_service import (
    LOW_DISK_THRESHOLD_BYTES,
    ORPHAN_MIN_AGE_MS,
    StorageService,
)
from odysseus_desktop_backend.services.vector_store import SQLiteNumPyVectorStore
from odysseus_desktop_backend.storage import Database, utc_ms


PRIVATE_SENTINEL = "PRIVATE_STORAGE_SENTINEL_MUST_NOT_APPEAR"
FIXTURE_TEXT = (
    "Storage cleanup reclaims derived data honestly and never touches user files."
)


class Services:
    def __init__(self, profile_dir: Path, has_active_jobs=None):
        self.db = Database(profile_dir)
        self.documents = DocumentService(self.db)
        self.embeddings = EmbeddingService(self.db, provider=LocalHashEmbeddingProvider())
        self.vector_store = SQLiteNumPyVectorStore(self.db)
        self.rag = RAGService(self.documents, self.embeddings, self.vector_store)
        self.artifacts = ArtifactService(self.db, self.documents, self.rag)
        self.sources = SourceService(self.documents, self.artifacts, self.rag)
        self.storage = StorageService(
            self.db, self.documents, self.artifacts, has_active_jobs=has_active_jobs
        )

    def close(self) -> None:
        self.db.close()


@pytest.fixture()
def services(tmp_path: Path):
    built = Services(tmp_path / "profile")
    yield built
    built.close()


def make_fixture(tmp_path: Path, name: str = "note.txt", text: str = FIXTURE_TEXT) -> Path:
    path = tmp_path / name
    path.write_text(text, encoding="utf-8")
    return path


def import_indexed(services: Services, fixture: Path) -> dict:
    document = services.documents.import_document(str(fixture))
    services.rag.index_document(document["id"])
    return services.documents.get(document["id"])


def walk_bytes(root: Path) -> int:
    total = 0
    for dirpath, _dirnames, filenames in os.walk(root):
        for name in filenames:
            path = Path(dirpath) / name
            if not path.is_symlink():
                total += path.stat().st_size
    return total


def age_file(path: Path, *, minutes: int = 30) -> None:
    stamp = path.stat().st_mtime - minutes * 60
    os.utime(path, (stamp, stamp))


def try_make_junction(link: Path, target: Path) -> bool:
    if not sys.platform.startswith("win"):
        return False
    result = subprocess.run(
        ["cmd", "/c", "mklink", "/J", str(link), str(target)],
        capture_output=True,
    )
    return result.returncode == 0 and link.exists()


# ----------------------------------------------------------- accounting (§10)


def test_profile_size_matches_disk_fixture(services: Services, tmp_path: Path):
    import_indexed(services, make_fixture(tmp_path))
    (services.db.profile_dir / "logs").mkdir(exist_ok=True)
    (services.db.profile_dir / "logs" / "backend.log").write_text("x" * 1234, encoding="utf-8")
    services.db.conn.execute("PRAGMA wal_checkpoint(FULL)")
    status = services.storage.status()
    assert status["total_bytes"] == walk_bytes(services.db.profile_dir)
    assert status["skipped_count"] == 0


def test_category_totals_reconcile_and_db_wal_shm_accounted(services: Services, tmp_path: Path):
    import_indexed(services, make_fixture(tmp_path))
    status = services.storage.status()
    categories = status["categories"]
    assert set(categories) == {"database", "documents", "images", "logs", "models", "other"}
    assert status["total_bytes"] == sum(bucket["bytes"] for bucket in categories.values())
    # WAL mode is active, so database bytes must cover app.db plus any -wal/-shm.
    db_files = sum(
        (services.db.profile_dir / name).stat().st_size
        for name in ("app.db", "app.db-wal", "app.db-shm")
        if (services.db.profile_dir / name).exists()
    )
    assert categories["database"]["bytes"] == db_files
    stored = Path(services.documents.list()[0]["stored_path"])
    assert categories["documents"]["bytes"] >= stored.stat().st_size


def test_unknown_profile_files_count_as_other_and_models_never_cleanable(
    services: Services, tmp_path: Path
):
    models_dir = services.db.profile_dir / "models" / "florence2-pack"
    models_dir.mkdir(parents=True)
    model_file = models_dir / "weights.bin"
    model_file.write_bytes(b"m" * 2048)
    unknown = services.db.profile_dir / f"{PRIVATE_SENTINEL}.dat"
    unknown.write_bytes(b"u" * 512)
    age_file(model_file)
    age_file(unknown)
    status = services.storage.status()
    assert status["categories"]["models"]["bytes"] == 2048
    assert status["categories"]["other"]["bytes"] >= 512
    preview = services.storage.cleanup_preview()
    result = services.storage.cleanup()
    assert model_file.exists()
    assert unknown.exists()
    assert preview["orphan_files"]["files"] == 0
    assert result["orphan_files"]["files"] == 0


def test_low_disk_threshold_boundary(services: Services, monkeypatch: pytest.MonkeyPatch):
    import shutil as shutil_module

    class Usage:
        def __init__(self, free):
            self.total = 100 * LOW_DISK_THRESHOLD_BYTES
            self.used = self.total - free
            self.free = free

    monkeypatch.setattr(
        shutil_module, "disk_usage", lambda _path: Usage(LOW_DISK_THRESHOLD_BYTES)
    )
    status = services.storage.status()
    assert status["low_disk"] is False
    assert status["free_disk_bytes"] == LOW_DISK_THRESHOLD_BYTES

    monkeypatch.setattr(
        shutil_module, "disk_usage", lambda _path: Usage(LOW_DISK_THRESHOLD_BYTES - 1)
    )
    status = services.storage.status()
    assert status["low_disk"] is True

    def raise_oserror(_path):
        raise OSError("disk usage unavailable")

    monkeypatch.setattr(shutil_module, "disk_usage", raise_oserror)
    status = services.storage.status()
    assert status["free_disk_bytes"] == -1
    assert status["low_disk"] is False


def test_inaccessible_entries_are_skipped_not_zeroed(
    services: Services, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
):
    import_indexed(services, make_fixture(tmp_path))
    blocked = services.db.profile_dir / "files" / "documents"
    real_scandir = os.scandir

    def guarded_scandir(path):
        if Path(path) == blocked.resolve() or Path(path) == blocked:
            raise PermissionError("blocked for test")
        return real_scandir(path)

    monkeypatch.setattr(os, "scandir", guarded_scandir)
    status = services.storage.status()
    assert status["skipped_count"] >= 1
    assert status["categories"]["documents"]["bytes"] == 0


def test_scan_never_follows_symlink_or_junction_escape(services: Services, tmp_path: Path):
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "huge.bin").write_bytes(b"z" * 65536)
    baseline = services.storage.status()["total_bytes"]

    junction = services.db.profile_dir / "files" / "escape"
    made = try_make_junction(junction, outside)
    if not made:
        pytest.skip("junction creation unavailable on this machine")
    assert is_link(junction) is True
    status = services.storage.status()
    # The junction target's bytes are never counted; the entry is skipped.
    assert status["total_bytes"] == baseline
    assert status["skipped_count"] >= 1
    result = services.storage.cleanup()
    assert (outside / "huge.bin").exists()
    assert result["orphan_files"]["files"] == 0


def test_status_is_deterministic(services: Services, tmp_path: Path):
    import_indexed(services, make_fixture(tmp_path))
    first = services.storage.status()
    second = services.storage.status()
    assert first["total_bytes"] == second["total_bytes"]
    assert first["categories"] == second["categories"]


# ------------------------------------------------------- source deletion (§6)


def test_delete_reclaims_file_rows_and_leaves_original(services: Services, tmp_path: Path):
    fixture = make_fixture(tmp_path, text=FIXTURE_TEXT + " " + PRIVATE_SENTINEL)
    document = import_indexed(services, fixture)
    stored = Path(document["stored_path"])
    stored_size = stored.stat().st_size
    assert stored.exists()

    result = services.rag.delete_document(document["id"])

    assert result["deleted"] is True
    assert result["already_deleted"] is False
    assert result["tombstoned"] is False
    assert result["file_removed"] is True
    assert result["bytes_reclaimed"] == stored_size
    assert result["deleted_chunks"] >= 1
    assert result["deleted_pages"] >= 1
    assert not stored.exists()
    # The user's original external file is untouched.
    assert fixture.exists()
    assert fixture.read_text(encoding="utf-8").startswith(FIXTURE_TEXT)
    # No rows survive anywhere.
    for table in ("documents", "rag_chunks", "document_pages", "ocr_pages"):
        count = services.db.conn.execute(
            f"SELECT COUNT(*) AS n FROM {table}"
        ).fetchone()["n"]
        assert count == 0, table
    assert services.rag.search("storage cleanup", limit=3) == []


def test_delete_is_idempotent_with_fixed_already_gone_shape(services: Services, tmp_path: Path):
    document = import_indexed(services, make_fixture(tmp_path))
    first = services.rag.delete_document(document["id"])
    assert first["deleted"] is True
    second = services.rag.delete_document(document["id"])
    assert second["deleted"] is True
    assert second["already_deleted"] is True
    assert second["bytes_reclaimed"] == 0
    assert second["file_removed"] is False
    assert second["file_missing"] is True


def test_delete_tombstones_when_chat_references_but_still_reclaims(
    services: Services, tmp_path: Path
):
    document = import_indexed(services, make_fixture(tmp_path))
    stored = Path(document["stored_path"])
    now = utc_ms()
    services.db.conn.execute(
        "INSERT INTO sessions(id, title, model, created_at, updated_at) VALUES ('s1', 'Chat', '', ?, ?)",
        (now, now),
    )
    services.db.conn.execute(
        "INSERT INTO messages(id, session_id, role, content, created_at) VALUES ('m1', 's1', 'user', 'hello', ?)",
        (now,),
    )
    services.db.conn.execute(
        "INSERT INTO message_documents(message_id, document_id, created_at) VALUES ('m1', ?, ?)",
        (document["id"], now),
    )
    services.db.conn.commit()

    result = services.rag.delete_document(document["id"])
    assert result["tombstoned"] is True
    assert result["file_removed"] is True
    assert not stored.exists()
    row = services.db.conn.execute(
        "SELECT is_deleted, status FROM documents WHERE id = ?", (document["id"],)
    ).fetchone()
    assert row["is_deleted"] == 1
    assert row["status"] == "deleted"
    # Derived content-bearing rows are still purged.
    chunks = services.db.conn.execute(
        "SELECT COUNT(*) AS n FROM rag_chunks WHERE document_id = ?", (document["id"],)
    ).fetchone()["n"]
    assert chunks == 0
    # Chat hydration still renders the deleted attachment label.
    grouped = services.documents.attachments_for_messages(["m1"])
    assert grouped["m1"][0]["status_label"] == "attachment deleted"


def test_malicious_stored_path_outside_profile_is_refused(services: Services, tmp_path: Path):
    victim = tmp_path / "victim.txt"
    victim.write_text("user file that must survive", encoding="utf-8")
    document = import_indexed(services, make_fixture(tmp_path))
    services.db.conn.execute(
        "UPDATE documents SET stored_path = ? WHERE id = ?",
        (str(victim), document["id"]),
    )
    services.db.conn.commit()

    result = services.rag.delete_document(document["id"])
    assert result["deleted"] is True
    assert result["file_removed"] is False
    assert result["bytes_reclaimed"] == 0
    assert victim.exists()


def test_symlinked_stored_path_is_refused(services: Services, tmp_path: Path):
    victim = tmp_path / "victim.bin"
    victim.write_bytes(b"v" * 100)
    document = import_indexed(services, make_fixture(tmp_path))
    stored = Path(document["stored_path"])
    stored.unlink()
    try:
        stored.symlink_to(victim)
    except OSError:
        pytest.skip("symlink creation requires privileges on this machine")
    result = services.rag.delete_document(document["id"])
    assert result["file_removed"] is False
    assert victim.exists()


def test_locked_file_failure_is_honest_and_orphan_recoverable(
    services: Services, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    document = import_indexed(services, make_fixture(tmp_path))
    stored = Path(document["stored_path"])

    real_unlink = Path.unlink

    def failing_unlink(self, *args, **kwargs):
        if self.name == stored.name:
            raise PermissionError("file is locked")
        return real_unlink(self, *args, **kwargs)

    monkeypatch.setattr(Path, "unlink", failing_unlink)
    result = services.rag.delete_document(document["id"])
    monkeypatch.undo()

    # Honest reporting: rows purged, file failure not claimed as reclaimed.
    assert result["deleted"] is True
    assert result["file_removed"] is False
    assert result["bytes_reclaimed"] == 0
    assert stored.exists()
    assert services.documents.list() == []

    # The stranded copy is now an orphan; cleanup reclaims it.
    age_file(stored)
    preview = services.storage.cleanup_preview()
    assert preview["orphan_files"]["files"] == 1
    cleanup = services.storage.cleanup()
    assert cleanup["orphan_files"]["files"] == 1
    assert not stored.exists()


class FailingConnection:
    """Proxy that fails a chosen statement once; sqlite3.Connection attributes
    are read-only, so injection wraps the connection object instead."""

    def __init__(self, conn: sqlite3.Connection, fail_prefix: str):
        self._conn = conn
        self._fail_prefix = fail_prefix
        self.failures = 0

    def execute(self, sql, *args):
        if sql.strip().startswith(self._fail_prefix):
            self.failures += 1
            raise sqlite3.OperationalError("database is locked")
        return self._conn.execute(sql, *args)

    def __getattr__(self, name):
        return getattr(self._conn, name)


def test_db_failure_does_not_produce_false_deleted_state(
    services: Services, tmp_path: Path
):
    document = import_indexed(services, make_fixture(tmp_path))
    stored = Path(document["stored_path"])
    real_conn = services.db.conn
    failing = FailingConnection(real_conn, "DELETE FROM rag_chunks")
    services.db.conn = failing  # type: ignore[assignment]
    try:
        with pytest.raises(RuntimeError, match="delete_failed"):
            services.rag.delete_document(document["id"])
        assert failing.failures == 1
    finally:
        services.db.conn = real_conn

    # Nothing was deleted; the source is intact and the delete is retryable.
    assert stored.exists()
    assert services.documents.get(document["id"])["is_deleted"] is False
    retry = services.rag.delete_document(document["id"])
    assert retry["file_removed"] is True
    assert not stored.exists()


def test_artifact_delete_reclaims_files_and_internal_documents(services: Services, tmp_path: Path):
    pytest.importorskip("PIL")
    from PIL import Image

    image_path = tmp_path / "photo.png"
    Image.new("RGB", (64, 48), (10, 120, 60)).save(image_path)
    artifact = services.artifacts.import_path(str(image_path))
    artifact_id = str(artifact["id"])
    files = [
        Path(d["stored_path"])
        for d in services.artifacts.derivations(artifact_id)
        if d.get("stored_path")
    ]
    assert files
    result = services.artifacts.delete(artifact_id)
    assert result["deleted"] is True
    assert result["tombstoned"] is False
    assert result["files_removed"] >= len(files)
    assert result["bytes_reclaimed"] > 0
    for path in files:
        assert not path.exists()
    assert image_path.exists()
    repeat = services.artifacts.delete(artifact_id)
    assert repeat["already_deleted"] is True
    assert repeat["bytes_reclaimed"] == 0


# ----------------------------------------------------------------- cleanup (§9)


def test_cache_cleanup_removes_only_unused_rows(services: Services, tmp_path: Path):
    keep = import_indexed(services, make_fixture(tmp_path, "keep.txt", "Keep this document."))
    drop = import_indexed(services, make_fixture(tmp_path, "drop.txt", "Drop this document."))
    services.rag.delete_document(drop["id"])
    before = services.db.conn.execute(
        "SELECT COUNT(*) AS n FROM embedding_cache"
    ).fetchone()["n"]
    preview = services.storage.cleanup_preview()
    result = services.storage.cleanup()
    after = services.db.conn.execute(
        "SELECT COUNT(*) AS n FROM embedding_cache"
    ).fetchone()["n"]
    assert result["embedding_cache"]["rows"] == preview["embedding_cache"]["rows"]
    assert after == before - result["embedding_cache"]["rows"]
    # Every remaining row still backs a live chunk of the kept document.
    assert services.rag.search("keep document", limit=2)
    assert services.documents.get(keep["id"])["is_deleted"] is False


def test_orphan_cleanup_preview_matches_execution_and_respects_age(
    services: Services, tmp_path: Path
):
    documents_dir = services.documents.documents_dir
    old_orphan = documents_dir / f"{uuid.uuid4()}.txt"
    old_orphan.write_text("orphan", encoding="utf-8")
    age_file(old_orphan)
    fresh = documents_dir / f"{uuid.uuid4()}.txt"
    fresh.write_text("fresh file, maybe mid-import", encoding="utf-8")

    live = import_indexed(services, make_fixture(tmp_path))
    live_file = Path(live["stored_path"])
    age_file(live_file)

    preview = services.storage.cleanup_preview()
    assert preview["orphan_files"]["files"] == 1
    result = services.storage.cleanup()
    assert result["orphan_files"]["files"] == 1
    assert not old_orphan.exists()
    assert fresh.exists(), "files younger than ORPHAN_MIN_AGE_MS are never candidates"
    assert live_file.exists(), "files referenced by live rows are never candidates"
    assert ORPHAN_MIN_AGE_MS >= 60_000


def test_cleanup_never_touches_db_logs_or_subdirectories(services: Services, tmp_path: Path):
    logs_dir = services.db.profile_dir / "logs"
    logs_dir.mkdir(exist_ok=True)
    log_file = logs_dir / "backend.log"
    log_file.write_text("fixed labels only", encoding="utf-8")
    age_file(log_file)
    nested = services.documents.documents_dir / "nested"
    nested.mkdir()
    nested_file = nested / "keep.bin"
    nested_file.write_bytes(b"n" * 64)
    age_file(nested_file)
    services.db.conn.execute("PRAGMA wal_checkpoint(FULL)")

    services.storage.cleanup()
    assert (services.db.profile_dir / "app.db").exists()
    assert log_file.exists()
    assert nested_file.exists(), "cleanup never recurses into unknown subdirectories"


def test_legacy_soft_deleted_rows_are_purged(services: Services, tmp_path: Path):
    document = import_indexed(services, make_fixture(tmp_path))
    stored = Path(document["stored_path"])
    # Fabricate a pre-v0.4 soft delete: rows flagged, file left behind.
    now = utc_ms()
    services.db.conn.execute(
        "UPDATE documents SET is_deleted = 1, status = 'deleted', index_status = 'deleted', updated_at = ? WHERE id = ?",
        (now, document["id"]),
    )
    services.db.conn.execute(
        "UPDATE rag_chunks SET is_deleted = 1 WHERE document_id = ?", (document["id"],)
    )
    services.db.conn.commit()
    age_file(stored)

    preview = services.storage.cleanup_preview()
    assert preview["legacy_deleted_sources"]["documents"] == 1
    assert preview["legacy_deleted_sources"]["files"] == 1
    result = services.storage.cleanup()
    assert result["legacy_deleted_sources"]["documents"] == 1
    assert not stored.exists()
    remaining = services.db.conn.execute(
        "SELECT COUNT(*) AS n FROM documents"
    ).fetchone()["n"]
    assert remaining == 0
    repeat = services.storage.cleanup()
    assert repeat["legacy_deleted_sources"]["documents"] == 0
    assert repeat["reclaimed_file_bytes"] == 0


def test_cleanup_refused_while_jobs_active(tmp_path: Path):
    services = Services(tmp_path / "profile", has_active_jobs=lambda: True)
    try:
        with pytest.raises(RuntimeError, match="cleanup_busy"):
            services.storage.cleanup()
        # Reads stay available during jobs.
        assert services.storage.status()["total_bytes"] >= 0
        assert services.storage.cleanup_preview()["orphan_files"]["files"] == 0
    finally:
        services.close()


# ------------------------------------------------------------ active jobs (§8)


def test_release_source_cancels_active_job_and_bounded_waits(tmp_path: Path):
    import threading

    from odysseus_desktop_backend.cancellation import check_cancelled

    release_gate = threading.Event()
    started_gate = threading.Event()

    class SlowExecutor:
        def run(self, job, on_running):
            on_running()
            started_gate.set()
            release_gate.wait(timeout=10)
            check_cancelled()

        def rollback(self, job):
            pass

        def close(self):
            pass

    jobs = JobService(tmp_path, executor_factory=SlowExecutor)
    try:
        snapshot = jobs.submit_ocr("doc-1")
        assert started_gate.wait(timeout=5), "job must be mid-run before release"
        # Bounded failure first: the job ignores cancel until the gate opens.
        assert jobs.release_source(document_id="doc-1", timeout=0.3) is False
        assert jobs.get(snapshot["job_id"])["state"] == "cancel_requested"
        # Open the gate: the same call now succeeds within the wait budget.
        release_gate.set()
        assert jobs.release_source(document_id="doc-1", timeout=5.0) is True
        assert jobs.get(snapshot["job_id"])["state"] in {"cancelled", "completed", "failed"}
        # Releasing a source with no jobs is trivially true.
        assert jobs.release_source(document_id="other") is True
    finally:
        jobs.shutdown()


def test_rpc_delete_returns_fixed_busy_code(tmp_path: Path):
    """The RPC layer maps an unreleased source to the fixed source_busy code."""
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    from rpc_server import RpcError, SidecarApp

    app = SidecarApp(tmp_path / "profile")
    try:
        app.jobs.release_source = lambda **_kwargs: False  # type: ignore[method-assign]
        with pytest.raises(RpcError) as excinfo:
            app.dispatch("documents.delete", {"document_id": "doc-1"})
        assert excinfo.value.message == "source_busy"
        assert PRIVATE_SENTINEL not in str(excinfo.value.message)
    finally:
        app.close()


# ------------------------------------------------------------- RPC + privacy


def test_storage_rpcs_dispatch_and_shapes(tmp_path: Path):
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    from rpc_server import SidecarApp

    fixture = tmp_path / f"{PRIVATE_SENTINEL}.txt"
    fixture.write_text(FIXTURE_TEXT, encoding="utf-8")
    app = SidecarApp(tmp_path / "profile")
    try:
        app.dispatch("documents.import", {"path": str(fixture)})
        status = app.dispatch("storage.status", {})
        assert status["total_bytes"] > 0
        assert status["profile_dir"] == str(tmp_path / "profile")
        assert isinstance(status["low_disk"], bool)
        preview = app.dispatch("storage.cleanup_preview", {})
        assert set(preview) >= {
            "embedding_cache",
            "orphan_files",
            "legacy_deleted_sources",
            "reclaimable_file_bytes",
            "reclaimable_db_bytes",
        }
        result = app.dispatch("storage.cleanup", {})
        assert set(result) >= {
            "reclaimed_file_bytes",
            "reclaimed_db_bytes",
            "skipped_count",
            "failed_count",
        }
    finally:
        app.close()


def test_storage_payloads_and_logs_never_leak_names(tmp_path: Path, caplog):
    """Sentinel sweep: hostile file names never appear in aggregates or logs."""
    import logging

    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    import json as json_module

    from rpc_server import SidecarApp

    fixture = tmp_path / f"{PRIVATE_SENTINEL}.txt"
    fixture.write_text(FIXTURE_TEXT + " " + PRIVATE_SENTINEL, encoding="utf-8")
    app = SidecarApp(tmp_path / "profile")
    try:
        imported = app.dispatch("documents.import", {"path": str(fixture)})
        document_id = imported["document"]["id"]
        with caplog.at_level(logging.INFO, logger="odysseus_desktop_backend.storage"):
            status = app.dispatch("storage.status", {})
            preview = app.dispatch("storage.cleanup_preview", {})
            deleted = app.dispatch("documents.delete", {"document_id": document_id})
            cleanup = app.dispatch("storage.cleanup", {})
        # The profile root is the single allowed path in storage.status.
        for payload in (preview, deleted, cleanup):
            assert PRIVATE_SENTINEL not in json_module.dumps(payload)
        status_without_root = {k: v for k, v in status.items() if k != "profile_dir"}
        assert PRIVATE_SENTINEL not in json_module.dumps(status_without_root)
        storage_logs = "\n".join(
            record.getMessage()
            for record in caplog.records
            if record.name.startswith("odysseus_desktop_backend.storage")
            or record.name.startswith("odysseus_desktop_backend.documents")
        )
        assert PRIVATE_SENTINEL not in storage_logs
    finally:
        app.close()


def test_pathsafety_is_link_guard(tmp_path: Path):
    regular = tmp_path / "plain.txt"
    regular.write_text("x", encoding="utf-8")
    assert is_link(regular) is False
    assert is_link(tmp_path / "missing.txt") is None
    target = tmp_path / "target"
    target.mkdir()
    junction = tmp_path / "junction"
    if try_make_junction(junction, target):
        assert is_link(junction) is True
