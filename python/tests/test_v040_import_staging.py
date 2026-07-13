"""Issue #16 — import staging, purge, and startup repair
(V04_ESSENTIAL_SEMANTICS.md §B).

Scratch profiles and synthetic fixtures only. The embedding provider is
forced to the deterministic lexical fallback so no Ollama is involved.
"""

from __future__ import annotations

import sqlite3
import subprocess
from pathlib import Path

import pytest

from odysseus_desktop_backend.services.artifact_service import ArtifactService
from odysseus_desktop_backend.services.document_service import DocumentService
from odysseus_desktop_backend.services.embedding_service import (
    EmbeddingService,
    LocalHashEmbeddingProvider,
)
from odysseus_desktop_backend.services.rag_service import RAGService
from odysseus_desktop_backend.services.source_service import SourceService
from odysseus_desktop_backend.services.vector_store import SQLiteNumPyVectorStore
from odysseus_desktop_backend.storage import Database


PRIVATE_SENTINEL = "PRIVATE_STAGING_SENTINEL_MUST_NOT_APPEAR"
FIXTURE_TEXT = "The staging contract hides uncommitted imports from every user surface."


class Services:
    def __init__(self, profile_dir: Path):
        self.db = Database(profile_dir)
        self.documents = DocumentService(self.db)
        self.embeddings = EmbeddingService(self.db, provider=LocalHashEmbeddingProvider())
        self.vector_store = SQLiteNumPyVectorStore(self.db)
        self.rag = RAGService(self.documents, self.embeddings, self.vector_store)
        self.artifacts = ArtifactService(self.db, self.documents, self.rag)
        self.sources = SourceService(self.documents, self.artifacts, self.rag)

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


def import_staged_and_indexed(services: Services, fixture: Path) -> str:
    document = services.documents.import_document(str(fixture), staging=True)
    services.rag.index_document(document["id"])
    return str(document["id"])


# ------------------------------------------------------------------- schema


def test_fresh_schema_has_is_staging_default_zero(tmp_path: Path):
    db = Database(tmp_path)
    try:
        columns = {row["name"]: row for row in db.conn.execute("PRAGMA table_info(documents)")}
        assert "is_staging" in columns
        assert columns["is_staging"]["dflt_value"] == "0"
    finally:
        db.close()


def test_upgrade_from_v021_gets_is_staging_and_old_rows_default_non_staging(tmp_path: Path):
    fixture_sql = Path(__file__).parent / "fixtures" / "v021_schema.sql"
    profile = tmp_path / "upgrade"
    profile.mkdir(parents=True)
    with sqlite3.connect(profile / "app.db") as conn:
        conn.executescript(fixture_sql.read_text(encoding="utf-8"))
        conn.execute(
            """
            INSERT INTO documents(
                id, title, source_path, stored_path, file_name, file_type,
                content_hash, size_bytes, created_at, updated_at
            )
            VALUES ('old-doc', 'Old document', 'src.txt', 'stored.txt',
                    'src.txt', 'txt', 'hash', 10, 1, 1)
            """
        )
    db = Database(profile)
    try:
        row = db.conn.execute(
            "SELECT is_staging FROM documents WHERE id = 'old-doc'"
        ).fetchone()
        assert row["is_staging"] == 0
        documents = DocumentService(db)
        listed = documents.list(scope=None)
        assert [item["id"] for item in listed] == ["old-doc"]
        assert listed[0]["is_staging"] is False
    finally:
        db.close()


def test_init_schema_idempotent_with_is_staging(tmp_path: Path):
    db = Database(tmp_path)
    try:
        db.init_schema()
        db.init_schema()
        count = sum(
            1
            for row in db.conn.execute("PRAGMA table_info(documents)")
            if row["name"] == "is_staging"
        )
        assert count == 1
    finally:
        db.close()


# --------------------------------------------------------------- visibility


def test_staging_document_hidden_from_documents_list(services: Services, tmp_path: Path):
    document_id = import_staged_and_indexed(services, make_fixture(tmp_path))
    # get() by id still works (job API path).
    fetched = services.documents.get(document_id)
    assert fetched["is_staging"] is True
    assert fetched["index_status"] == "indexed"  # staging reaches indexed pre-commit
    assert services.documents.list() == []
    assert services.documents.list(scope=None, include_deleted=True) == []


def test_staging_document_hidden_from_sources_list(services: Services, tmp_path: Path):
    import_staged_and_indexed(services, make_fixture(tmp_path))
    assert services.sources.list(scope=None, include_session=True) == []


def test_staging_document_hidden_from_vector_search(services: Services, tmp_path: Path):
    document_id = import_staged_and_indexed(services, make_fixture(tmp_path))
    assert services.rag.search("staging contract hides uncommitted imports") == []
    # Commit is the visibility boundary: the same query now returns it.
    services.documents.commit_staging(document_id)
    results = services.rag.search("staging contract hides uncommitted imports")
    assert results and results[0]["document_id"] == document_id


def test_staging_file_lives_only_inside_profile(services: Services, tmp_path: Path):
    fixture = make_fixture(tmp_path)
    document_id = import_staged_and_indexed(services, fixture)
    stored = Path(services.documents.get(document_id)["stored_path"])
    assert stored.exists()
    assert stored.parent == services.documents.documents_dir
    assert services.documents.documents_dir.is_relative_to(services.db.profile_dir)


def test_commit_makes_document_visible_and_is_idempotent(services: Services, tmp_path: Path):
    document_id = import_staged_and_indexed(services, make_fixture(tmp_path))
    committed = services.documents.commit_staging(document_id)
    assert committed["is_staging"] is False
    assert [item["id"] for item in services.documents.list()] == [document_id]
    again = services.documents.commit_staging(document_id)
    assert again["is_staging"] is False
    with pytest.raises(KeyError):
        services.documents.commit_staging("missing-id")


# -------------------------------------------------------------------- purge


def test_purge_removes_rows_and_owned_file(services: Services, tmp_path: Path):
    fixture = make_fixture(tmp_path)
    document_id = import_staged_and_indexed(services, fixture)
    stored = Path(services.documents.get(document_id)["stored_path"])
    stored_size = stored.stat().st_size
    assert services.documents.chunks(document_id)

    result = services.documents.purge_document(document_id)

    assert result["purged"] is True
    assert result["file_removed"] is True
    assert result["bytes_reclaimed"] == stored_size
    assert not stored.exists()
    with pytest.raises(KeyError):
        services.documents.get(document_id)
    for table in ("document_pages", "ocr_pages", "rag_chunks"):
        rows = services.db.conn.execute(
            f"SELECT COUNT(*) AS count FROM {table} WHERE document_id = ?",
            (document_id,),
        ).fetchone()
        assert rows["count"] == 0
    # Original external fixture is untouched.
    assert fixture.exists()
    assert fixture.read_text(encoding="utf-8") == FIXTURE_TEXT


def test_repeated_purge_is_idempotent(services: Services, tmp_path: Path):
    document_id = import_staged_and_indexed(services, make_fixture(tmp_path))
    first = services.documents.purge_document(document_id)
    second = services.documents.purge_document(document_id)
    assert first["purged"] is True
    assert second == {
        "purged": False,
        "file_removed": False,
        "file_missing": False,
        "bytes_reclaimed": 0,
        "document_id": document_id,
    }


def test_purge_with_missing_owned_file_is_honest(services: Services, tmp_path: Path):
    document_id = import_staged_and_indexed(services, make_fixture(tmp_path))
    Path(services.documents.get(document_id)["stored_path"]).unlink()
    result = services.documents.purge_document(document_id)
    assert result["purged"] is True
    assert result["file_removed"] is False
    assert result["file_missing"] is True
    assert result["bytes_reclaimed"] == 0


def test_purge_refuses_stored_path_outside_profile(services: Services, tmp_path: Path):
    """A hostile stored_path pointing at the user's file must fail closed."""
    external = make_fixture(tmp_path, "precious-original.txt", "irreplaceable user data")
    document_id = import_staged_and_indexed(services, make_fixture(tmp_path))
    stored = Path(services.documents.get(document_id)["stored_path"])
    services.db.conn.execute(
        "UPDATE documents SET stored_path = ? WHERE id = ?",
        (str(external), document_id),
    )
    services.db.conn.commit()

    result = services.documents.purge_document(document_id)

    assert result["purged"] is True  # rows are unambiguous app data
    assert result["file_removed"] is False
    assert external.exists()
    assert external.read_text(encoding="utf-8") == "irreplaceable user data"
    # The real copy is orphaned (reclaimed later by storage cleanup, #17).
    assert stored.exists()


def test_purge_refuses_traversal_stored_path(services: Services, tmp_path: Path):
    external = make_fixture(tmp_path, "outside.txt", "outside data")
    document_id = import_staged_and_indexed(services, make_fixture(tmp_path))
    traversal = (
        services.documents.documents_dir / ".." / ".." / ".." / ".." / external.name
    )
    services.db.conn.execute(
        "UPDATE documents SET stored_path = ? WHERE id = ?",
        (str(traversal), document_id),
    )
    services.db.conn.commit()
    result = services.documents.purge_document(document_id)
    assert result["file_removed"] is False
    assert external.exists()


def test_purge_refuses_junction_escape_where_creatable(services: Services, tmp_path: Path):
    """Windows directory junction inside documents dir pointing elsewhere."""
    outside_dir = tmp_path / "outside-dir"
    outside_dir.mkdir()
    victim = outside_dir / "victim.txt"
    victim.write_text("victim data", encoding="utf-8")
    junction = services.documents.documents_dir / "jx"
    made = subprocess.run(
        ["cmd", "/c", "mklink", "/J", str(junction), str(outside_dir)],
        capture_output=True,
    )
    if made.returncode != 0:
        pytest.skip("directory junction not creatable in this environment")

    document_id = import_staged_and_indexed(services, make_fixture(tmp_path))
    services.db.conn.execute(
        "UPDATE documents SET stored_path = ? WHERE id = ?",
        (str(junction / "victim.txt"), document_id),
    )
    services.db.conn.commit()

    result = services.documents.purge_document(document_id)

    assert result["file_removed"] is False
    assert victim.exists()
    assert victim.read_text(encoding="utf-8") == "victim data"


# ----------------------------------------------------------- startup repair


def test_startup_repair_removes_staged_leftovers(tmp_path: Path):
    profile = tmp_path / "profile"
    first = Services(profile)
    staged_id = import_staged_and_indexed(first, make_fixture(tmp_path, "staged.txt"))
    committed_id = import_staged_and_indexed(first, make_fixture(tmp_path, "kept.txt"))
    first.documents.commit_staging(committed_id)
    staged_file = Path(first.documents.get(staged_id)["stored_path"])
    first.close()  # simulates process death after close; rows persist

    second = Services(profile)
    try:
        repaired = second.documents.repair_startup_state()
        assert repaired["purged_staging"] == 1
        assert not staged_file.exists()
        with pytest.raises(KeyError):
            second.documents.get(staged_id)
        # Committed documents survive repair, fully intact.
        kept = second.documents.get(committed_id)
        assert kept["is_staging"] is False
        assert [item["id"] for item in second.documents.list()] == [committed_id]
        assert second.documents.chunks(committed_id)
        # Repair is idempotent.
        assert second.documents.repair_startup_state() == {
            "purged_staging": 0,
            "ocr_reset": 0,
        }
    finally:
        second.close()


def test_startup_repair_resets_stuck_ocr_running(tmp_path: Path):
    profile = tmp_path / "profile"
    services = Services(profile)
    try:
        document_id = import_staged_and_indexed(services, make_fixture(tmp_path))
        services.documents.commit_staging(document_id)
        services.documents.mark_ocr_running(document_id, "tesseract")
        repaired = services.documents.repair_startup_state()
        assert repaired["ocr_reset"] == 1
        assert services.documents.get(document_id)["ocr_status"] == "needed"
    finally:
        services.close()


def test_sidecar_startup_runs_repair(tmp_path: Path):
    from rpc_server import SidecarApp

    profile = tmp_path / "profile"
    seed = Services(profile)
    staged_id = import_staged_and_indexed(seed, make_fixture(tmp_path))
    staged_file = Path(seed.documents.get(staged_id)["stored_path"])
    seed.close()

    app = SidecarApp(profile)
    try:
        assert not staged_file.exists()
        assert app.dispatch("documents.list", {}) == []
        assert app.dispatch("rag.search", {"query": "staging contract", "limit": 3}) == []
    finally:
        app.close()


# ------------------------------------------------------------------ privacy


def test_purge_and_repair_outputs_contain_no_paths_or_sentinels(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
):
    profile = tmp_path / "profile"
    services = Services(profile)
    try:
        fixture = make_fixture(tmp_path, f"{PRIVATE_SENTINEL}.txt")
        document_id = import_staged_and_indexed(services, fixture)
        with caplog.at_level("DEBUG", logger="odysseus.documents"):
            caplog.clear()
            result = services.documents.purge_document(document_id)
            repaired = services.documents.repair_startup_state()
        for payload in (result, repaired):
            rendered = repr(payload)
            assert PRIVATE_SENTINEL not in rendered
            assert str(tmp_path) not in rendered
            assert "\\" not in rendered.replace("\\\\", "")  # no path fragments
        assert PRIVATE_SENTINEL not in caplog.text
        assert str(tmp_path) not in caplog.text
    finally:
        services.close()
