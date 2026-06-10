from __future__ import annotations

import json
import tempfile
import time
import uuid
from collections.abc import Callable
from pathlib import Path
from typing import Any

from odysseus_desktop_backend.logging_config import get_logger
from odysseus_desktop_backend.services.chat_service import ChatService
from odysseus_desktop_backend.services.document_service import DocumentService
from odysseus_desktop_backend.services.embedding_service import EmbeddingService
from odysseus_desktop_backend.services.model_service import OLLAMA_ENDPOINT, ModelService
from odysseus_desktop_backend.services.rag_service import RAGService
from odysseus_desktop_backend.services.session_service import SessionService
from odysseus_desktop_backend.services.settings_service import SettingsService
from odysseus_desktop_backend.services.vector_store import SQLiteNumPyVectorStore
from odysseus_desktop_backend.storage import Database, utc_ms


EVAL_SUITE_NAME = "local-rag"
EVAL_SUITE_VERSION = "v0.1.3"
logger = get_logger("evals")


class EvalModelService(ModelService):
    def __init__(self, db: Database, *, temperature: float):
        super().__init__(db)
        self.temperature = temperature

    def chat(self, model: str, messages: list[dict[str, str]]) -> str:
        payload = {
            "model": model,
            "messages": messages,
            "stream": False,
            "options": {"temperature": self.temperature},
        }
        data = self._post_json(f"{OLLAMA_ENDPOINT}/api/chat", payload, timeout=120)
        message = data.get("message") if isinstance(data, dict) else None
        if isinstance(message, dict):
            content = message.get("content")
            if isinstance(content, str):
                return content
        raise RuntimeError("Ollama returned no assistant message")


ModelServiceFactory = Callable[[Database, float], ModelService]


class EvalService:
    def __init__(
        self,
        db: Database,
        *,
        cases_dir: str | Path | None = None,
        model_service_factory: ModelServiceFactory | None = None,
    ):
        self.db = db
        self.cases_dir = Path(cases_dir) if cases_dir else default_cases_dir()
        self.model_service_factory = model_service_factory or (
            lambda temp_db, temperature: EvalModelService(temp_db, temperature=temperature)
        )

    def list_cases(self) -> dict[str, Any]:
        cases = load_cases(self.cases_dir)
        return {
            "suite_name": EVAL_SUITE_NAME,
            "suite_version": EVAL_SUITE_VERSION,
            "cases_dir": str(self.cases_dir),
            "case_count": len(cases),
            "cases": [case_summary(case) for case in cases],
        }

    def run(
        self,
        *,
        model: str,
        verify: bool = False,
        answer_style_override: str | None = None,
        temperature: float = 0.0,
    ) -> dict[str, Any]:
        model_name = (model or "").strip()
        if not model_name:
            raise ValueError("model is required")
        cases = load_cases(self.cases_dir)
        if not cases:
            raise RuntimeError(f"No eval cases found in {self.cases_dir}")

        started = time.perf_counter()
        case_results: list[dict[str, Any]] = []
        logger.info(
            "benchmark run started model=%s verify=%s cases=%s",
            model_name,
            verify,
            len(cases),
        )

        with tempfile.TemporaryDirectory(prefix=f"odysseus-rag-eval-{safe_name(model_name)}-") as temp:
            temp_db = Database(Path(temp))
            try:
                documents = DocumentService(temp_db)
                embeddings = EmbeddingService(temp_db)
                vector_store = SQLiteNumPyVectorStore(temp_db)
                rag = RAGService(documents, embeddings, vector_store)
                settings = SettingsService(temp_db)
                sessions = SessionService(temp_db)
                models = self.model_service_factory(temp_db, temperature)
                chat = ChatService(sessions, settings, models, rag=rag)
                document_ids = index_case_documents(cases, documents, rag)

                for case in cases:
                    case_started = time.perf_counter()
                    required_source = str(case["required_source_document"])
                    answer_style = str(answer_style_override or case.get("answer_style") or "precise")
                    result = chat.send(
                        str(case["question"]),
                        model=model_name,
                        use_rag=True,
                        document_ids=[document_ids[required_source]],
                        verify_rag=verify,
                        answer_style=answer_style,
                    )
                    elapsed_ms = int((time.perf_counter() - case_started) * 1000)
                    outcome = evaluate_case(case, result, document_ids[required_source])
                    case_results.append(
                        {
                            "case_id": str(case["id"]),
                            "question": str(case["question"]),
                            "answer_style": answer_style,
                            "required_source_document": required_source,
                            "passed": bool(outcome["passed"]),
                            "expected_passed": bool(outcome["expected_passed"]),
                            "forbidden_passed": bool(outcome["forbidden_passed"]),
                            "source_passed": bool(outcome["source_passed"]),
                            "latency_ms": elapsed_ms,
                            "reasons": list(outcome["reasons"]),
                        }
                    )
            finally:
                temp_db.close()

        total_runtime_ms = int((time.perf_counter() - started) * 1000)
        run = self._store_run(
            model=model_name,
            verify=verify,
            total_runtime_ms=total_runtime_ms,
            case_results=case_results,
        )
        logger.info(
            "benchmark run finished run_id=%s model=%s passed=%s failed=%s runtime_ms=%s",
            run["id"],
            model_name,
            run["total_passed"],
            run["total_failed"],
            total_runtime_ms,
        )
        return run

    def history(self, *, limit: int = 20) -> list[dict[str, Any]]:
        clean_limit = min(max(limit, 1), 100)
        rows = self.db.conn.execute(
            """
            SELECT *
            FROM benchmark_runs
            ORDER BY created_at DESC
            LIMIT ?
            """,
            (clean_limit,),
        ).fetchall()
        return [self._run_with_cases(row) for row in rows]

    def clear_history(self) -> dict[str, Any]:
        self.db.conn.execute("DELETE FROM benchmark_runs")
        self.db.conn.commit()
        logger.info("benchmark history cleared")
        return {"cleared": True}

    def _store_run(
        self,
        *,
        model: str,
        verify: bool,
        total_runtime_ms: int,
        case_results: list[dict[str, Any]],
    ) -> dict[str, Any]:
        now = utc_ms()
        run_id = str(uuid.uuid4())
        total_passed = sum(1 for case in case_results if case["passed"])
        total_failed = len(case_results) - total_passed
        average_latency_ms = (
            int(sum(int(case["latency_ms"]) for case in case_results) / len(case_results))
            if case_results
            else 0
        )
        self.db.conn.execute(
            """
            INSERT INTO benchmark_runs(
                id, model, verify, suite_name, suite_version, total_passed,
                total_failed, average_latency_ms, total_runtime_ms, notes, created_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, '', ?)
            """,
            (
                run_id,
                model,
                1 if verify else 0,
                EVAL_SUITE_NAME,
                EVAL_SUITE_VERSION,
                total_passed,
                total_failed,
                average_latency_ms,
                total_runtime_ms,
                now,
            ),
        )
        for case in case_results:
            self.db.conn.execute(
                """
                INSERT INTO benchmark_case_results(
                    id, run_id, case_id, question, answer_style, required_source_document,
                    passed, expected_passed, forbidden_passed, source_passed, latency_ms,
                    reasons_json, created_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    str(uuid.uuid4()),
                    run_id,
                    case["case_id"],
                    case["question"],
                    case["answer_style"],
                    case["required_source_document"],
                    1 if case["passed"] else 0,
                    1 if case["expected_passed"] else 0,
                    1 if case["forbidden_passed"] else 0,
                    1 if case["source_passed"] else 0,
                    int(case["latency_ms"]),
                    json.dumps(case["reasons"]),
                    now,
                ),
            )
        self.db.conn.commit()
        row = self.db.conn.execute("SELECT * FROM benchmark_runs WHERE id = ?", (run_id,)).fetchone()
        if row is None:
            raise RuntimeError("benchmark run was not stored")
        return self._run_with_cases(row)

    def _run_with_cases(self, row: Any) -> dict[str, Any]:
        run = benchmark_run_dict(row)
        case_rows = self.db.conn.execute(
            """
            SELECT *
            FROM benchmark_case_results
            WHERE run_id = ?
            ORDER BY created_at ASC, case_id ASC
            """,
            (run["id"],),
        ).fetchall()
        run["cases"] = [benchmark_case_dict(case_row) for case_row in case_rows]
        run["summary_markdown"] = format_benchmark_summary([run])
        return run


def default_cases_dir() -> Path:
    backend_root = Path(__file__).resolve().parents[3]
    return backend_root / "evals" / "rag_cases"


def load_cases(cases_dir: Path) -> list[dict[str, Any]]:
    cases: list[dict[str, Any]] = []
    for path in sorted(cases_dir.glob("*.json")):
        data = json.loads(path.read_text(encoding="utf-8"))
        data["_path"] = str(path)
        cases.append(data)
    return cases


def index_case_documents(
    cases: list[dict[str, Any]],
    documents: DocumentService,
    rag: RAGService,
) -> dict[str, str]:
    indexed: dict[str, str] = {}
    for case in cases:
        case_path = Path(str(case["_path"]))
        for document in case.get("documents", []):
            fixture_id = str(document["id"])
            if fixture_id in indexed:
                continue
            path = (case_path.parent / str(document["path"])).resolve()
            imported = documents.import_document(str(path))
            rag.index_document(imported["id"])
            indexed[fixture_id] = imported["id"]
    return indexed


def evaluate_case(case: dict[str, Any], result: dict[str, Any], required_document_id: str) -> dict[str, Any]:
    answer = normalize(result["assistant_message"]["content"])
    reasons: list[str] = []

    expected_passed = True
    for expected in case.get("expected_facts", []):
        phrases = [normalize(phrase) for phrase in expected.get("any", [])]
        if not any(phrase and phrase in answer for phrase in phrases):
            expected_passed = False
            reasons.append(f"missing expected fact: {expected.get('label')}")

    forbidden_passed = True
    for forbidden in case.get("forbidden_claims", []):
        phrases = [normalize(phrase) for phrase in forbidden.get("any", [])]
        matched = [phrase for phrase in phrases if phrase and phrase in answer]
        if matched:
            forbidden_passed = False
            reasons.append(f"forbidden claim present: {forbidden.get('label')}")

    retrieved = result.get("retrieved_snippets") or result.get("retrieved_chunks") or []
    source_passed = bool(retrieved) and all(
        item.get("document_id") == required_document_id for item in retrieved
    )
    if not source_passed:
        reasons.append("retrieved evidence did not stay within required source document")

    return {
        "passed": expected_passed and forbidden_passed and source_passed,
        "expected_passed": expected_passed,
        "forbidden_passed": forbidden_passed,
        "source_passed": source_passed,
        "reasons": reasons,
    }


def case_summary(case: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": str(case.get("id", "")),
        "question": str(case.get("question", "")),
        "answer_style": str(case.get("answer_style") or "precise"),
        "required_source_document": str(case.get("required_source_document", "")),
        "expected_fact_count": len(case.get("expected_facts", [])),
        "forbidden_claim_count": len(case.get("forbidden_claims", [])),
    }


def benchmark_run_dict(row: Any) -> dict[str, Any]:
    return {
        "id": row["id"],
        "model": row["model"],
        "verify": bool(row["verify"]),
        "suite_name": row["suite_name"],
        "suite_version": row["suite_version"],
        "total_passed": int(row["total_passed"]),
        "total_failed": int(row["total_failed"]),
        "average_latency_ms": int(row["average_latency_ms"]),
        "total_runtime_ms": int(row["total_runtime_ms"]),
        "notes": row["notes"],
        "created_at": int(row["created_at"]),
    }


def benchmark_case_dict(row: Any) -> dict[str, Any]:
    return {
        "id": row["id"],
        "run_id": row["run_id"],
        "case_id": row["case_id"],
        "question": row["question"],
        "answer_style": row["answer_style"],
        "required_source_document": row["required_source_document"],
        "passed": bool(row["passed"]),
        "expected_passed": bool(row["expected_passed"]),
        "forbidden_passed": bool(row["forbidden_passed"]),
        "source_passed": bool(row["source_passed"]),
        "latency_ms": int(row["latency_ms"]),
        "reasons": json.loads(row["reasons_json"] or "[]"),
        "created_at": int(row["created_at"]),
    }


def format_benchmark_summary(runs: list[dict[str, Any]]) -> str:
    lines = [
        "| Model | Verify | Passed | Failed | Avg latency | Notes |",
        "| --- | --- | ---: | ---: | ---: | --- |",
    ]
    for run in runs:
        failed_cases = [
            case["case_id"]
            for case in run.get("cases", [])
            if not case.get("passed")
        ]
        notes = "ok" if not failed_cases else "failed: " + ", ".join(failed_cases)
        lines.append(
            "| {model} | {verify} | {passed} | {failed} | {latency} ms | {notes} |".format(
                model=str(run.get("model", "")),
                verify="on" if run.get("verify") else "off",
                passed=int(run.get("total_passed", 0)),
                failed=int(run.get("total_failed", 0)),
                latency=int(run.get("average_latency_ms", 0)),
                notes=notes,
            )
        )
    return "\n".join(lines)


def normalize(text: str) -> str:
    return " ".join(
        (text or "").lower().replace("\u2013", "-").replace("\u00e2\u20ac\u201c", "-").split()
    )


def safe_name(value: str) -> str:
    return "".join(char if char.isalnum() else "-" for char in value).strip("-") or "model"
