from __future__ import annotations

import json
import tempfile
import time
import uuid
import math
from collections.abc import Callable
from pathlib import Path
from typing import Any
import re

from odysseus_desktop_backend.logging_config import get_logger
from odysseus_desktop_backend.services.chat_service import ChatService, DEFAULT_TEMPERATURE
from odysseus_desktop_backend.services.document_service import DocumentService
from odysseus_desktop_backend.services.embedding_service import DEFAULT_EMBEDDING_MODEL, EmbeddingService
from odysseus_desktop_backend.services.model_service import ModelService
from odysseus_desktop_backend.services.rag_service import RAGService
from odysseus_desktop_backend.services.session_service import SessionService
from odysseus_desktop_backend.services.settings_service import SettingsService
from odysseus_desktop_backend.services.vector_store import SQLiteNumPyVectorStore
from odysseus_desktop_backend.storage import Database, utc_ms


EVAL_SUITE_NAME = "local-rag"
EVAL_SUITE_VERSION = "v0.1.5"
WORD_RE = re.compile(r"[a-z0-9]+", re.IGNORECASE)
FACT_STOPWORDS = {
    "a",
    "an",
    "and",
    "are",
    "as",
    "at",
    "be",
    "by",
    "for",
    "from",
    "has",
    "have",
    "he",
    "her",
    "him",
    "his",
    "in",
    "is",
    "it",
    "of",
    "on",
    "or",
    "she",
    "that",
    "the",
    "their",
    "they",
    "this",
    "to",
    "was",
    "were",
    "with",
}
logger = get_logger("evals")


class EvalModelService(ModelService):
    def __init__(self, db: Database, *, temperature: float):
        super().__init__(db)
        self.temperature = temperature

    def chat(
        self,
        model: str,
        messages: list[dict[str, str]],
        *,
        options: dict[str, Any] | None = None,
    ) -> str:
        merged_options = {"temperature": self.temperature}
        if options:
            merged_options.update(options)
        return super().chat(model, messages, options=merged_options)


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
        temperature: float = DEFAULT_TEMPERATURE,
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
                copy_embedding_settings(self.db, temp_db)
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
                    required_document_id = document_ids[required_source]
                    answer_style = str(answer_style_override or case.get("answer_style") or "precise")
                    result = chat.send(
                        str(case["question"]),
                        model=model_name,
                        use_rag=True,
                        document_ids=document_scope_for_case(case, document_ids),
                        verify_rag=verify,
                        answer_style=answer_style,
                        temperature=temperature,
                    )
                    elapsed_ms = int((time.perf_counter() - case_started) * 1000)
                    embedding_status = rag.health().get("embedding", {})
                    retrieved_chunks = result.get("retrieved_chunks") or []
                    outcome = evaluate_case(case, result, required_document_id)
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
                            "retrieved_document_ids": retrieved_document_ids(retrieved_chunks),
                            "retrieved_chunk_ids": retrieved_chunk_ids(retrieved_chunks),
                            "embedding_backend": str(embedding_status.get("backend") or ""),
                            "embedding_model": str(embedding_status.get("model") or ""),
                            "temperature": float(temperature),
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
            temperature=temperature,
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

    def comparison(self, *, limit: int = 100) -> dict[str, Any]:
        runs = self.history(limit=limit)
        return benchmark_comparison(runs)

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
        temperature: float,
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
        embedding_backend = str(case_results[0].get("embedding_backend") or "") if case_results else ""
        embedding_model = str(case_results[0].get("embedding_model") or "") if case_results else ""
        self.db.conn.execute(
            """
            INSERT INTO benchmark_runs(
                id, model, verify, suite_name, suite_version, total_passed,
                total_failed, average_latency_ms, total_runtime_ms, notes,
                embedding_backend, embedding_model, temperature, created_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, '', ?, ?, ?, ?)
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
                embedding_backend,
                embedding_model,
                float(temperature),
                now,
            ),
        )
        for case in case_results:
            self.db.conn.execute(
                """
                INSERT INTO benchmark_case_results(
                    id, run_id, case_id, question, answer_style, required_source_document,
                    passed, expected_passed, forbidden_passed, source_passed, latency_ms,
                    reasons_json, retrieved_document_ids_json, retrieved_chunk_ids_json,
                    embedding_backend, embedding_model, temperature, created_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
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
                    json.dumps(case.get("retrieved_document_ids") or []),
                    json.dumps(case.get("retrieved_chunk_ids") or []),
                    str(case.get("embedding_backend") or ""),
                    str(case.get("embedding_model") or ""),
                    float(case.get("temperature") or 0),
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


def copy_embedding_settings(source_db: Database, target_db: Database) -> None:
    target_db.set_setting("embedding_backend", source_db.get_setting("embedding_backend", "auto"))
    target_db.set_setting(
        "embedding_model",
        source_db.get_setting("embedding_model", DEFAULT_EMBEDDING_MODEL),
    )


def document_scope_for_case(
    case: dict[str, Any],
    document_ids: dict[str, str],
) -> list[str] | None:
    scope = str(case.get("retrieval_scope") or case.get("document_scope") or "required").lower()
    if scope in {"all", "unscoped", "none"}:
        return None

    explicit = case.get("retrieval_documents")
    if isinstance(explicit, list):
        selected = [
            document_ids[str(item)]
            for item in explicit
            if str(item) in document_ids
        ]
        return selected or None

    required_source = str(case["required_source_document"])
    return [document_ids[required_source]]


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
        phrases = [str(phrase) for phrase in expected.get("any", [])]
        if not any(phrase_matches(answer, phrase, mode="expected") for phrase in phrases):
            expected_passed = False
            reasons.append(f"missing expected fact: {expected.get('label')}")

    forbidden_passed = True
    for forbidden in case.get("forbidden_claims", []):
        phrases = [str(phrase) for phrase in forbidden.get("any", [])]
        matched = [phrase for phrase in phrases if phrase_matches(answer, phrase, mode="forbidden")]
        if matched:
            forbidden_passed = False
            reasons.append(f"forbidden claim present: {forbidden.get('label')}")

    retrieved_chunks = result.get("retrieved_chunks") or []
    retrieved_snippets = result.get("retrieved_snippets") or []
    retrieved = retrieved_chunks or retrieved_snippets
    source_policy = str(case.get("source_policy") or "all_retrieved").lower()
    if source_policy == "top_retrieved":
        source_passed = bool(retrieved) and retrieved[0].get("document_id") == required_document_id
    else:
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
        "embedding_backend": row["embedding_backend"],
        "embedding_model": row["embedding_model"],
        "temperature": float(row["temperature"]),
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
        "retrieved_document_ids": json.loads(row["retrieved_document_ids_json"] or "[]"),
        "retrieved_chunk_ids": json.loads(row["retrieved_chunk_ids_json"] or "[]"),
        "embedding_backend": row["embedding_backend"],
        "embedding_model": row["embedding_model"],
        "temperature": float(row["temperature"]),
        "created_at": int(row["created_at"]),
    }


def format_benchmark_summary(runs: list[dict[str, Any]]) -> str:
    lines = [
        "| Model | Embeddings | Temp | Verify | Passed | Failed | Avg latency | Notes |",
        "| --- | --- | ---: | --- | ---: | ---: | ---: | --- |",
    ]
    for run in runs:
        failed_cases = [
            case["case_id"]
            for case in run.get("cases", [])
            if not case.get("passed")
        ]
        notes = "ok" if not failed_cases else "failed: " + ", ".join(failed_cases)
        lines.append(
            "| {model} | {embeddings} | {temp} | {verify} | {passed} | {failed} | {latency} ms | {notes} |".format(
                model=str(run.get("model", "")),
                embeddings=format_embedding_label(run),
                temp=f"{float(run.get('temperature') or 0):.2f}",
                verify="on" if run.get("verify") else "off",
                passed=int(run.get("total_passed", 0)),
                failed=int(run.get("total_failed", 0)),
                latency=int(run.get("average_latency_ms", 0)),
                notes=notes,
            )
        )
    return "\n".join(lines)


def benchmark_comparison(runs: list[dict[str, Any]]) -> dict[str, Any]:
    groups_by_key: dict[tuple[str, str, str, bool, float], dict[str, Any]] = {}
    for run in runs:
        key = comparison_key(run)
        group = groups_by_key.setdefault(
            key,
            {
                "key": "|".join(str(item) for item in key),
                "model": str(run.get("model") or ""),
                "embedding_backend": str(run.get("embedding_backend") or ""),
                "embedding_model": str(run.get("embedding_model") or ""),
                "verify": bool(run.get("verify")),
                "temperature": float(run.get("temperature") or 0),
                "suite_version": str(run.get("suite_version") or ""),
                "run_count": 0,
                "passed": 0,
                "total": 0,
                "expected_failures": 0,
                "forbidden_failures": 0,
                "source_failures": 0,
                "latency_total": 0,
                "total_runtime_ms": 0,
            },
        )
        cases = run.get("cases") or []
        group["run_count"] += 1
        group["passed"] += int(run.get("total_passed") or 0)
        group["total"] += len(cases)
        group["expected_failures"] += sum(1 for case in cases if not case.get("expected_passed"))
        group["forbidden_failures"] += sum(1 for case in cases if not case.get("forbidden_passed"))
        group["source_failures"] += sum(1 for case in cases if not case.get("source_passed"))
        group["latency_total"] += sum(int(case.get("latency_ms") or 0) for case in cases)
        group["total_runtime_ms"] += int(run.get("total_runtime_ms") or 0)

    for group in groups_by_key.values():
        total = int(group["total"])
        average_latency_ms = int(group["latency_total"] / total) if total else 0
        group.pop("latency_total", None)
        group["average_latency_ms"] = average_latency_ms
        group["pass_rate"] = (float(group["passed"]) / total) if total else 0.0

    groups = []
    for group in groups_by_key.values():
        group["guidance_labels"] = guidance_labels_for_group(group, groups_by_key)
        group["verifier_recommended"] = verifier_is_worthwhile(group, groups_by_key)
        groups.append(group)

    groups.sort(
        key=lambda group: (
            -int(group["passed"]),
            int(group["average_latency_ms"]),
            bool(group["verify"]),
            str(group["model"]),
        )
    )
    recommended = choose_recommended_group(groups, groups_by_key)
    if recommended:
        for group in groups:
            group["recommended"] = group["key"] == recommended["key"]
    return {
        "groups": groups,
        "recommended": recommended,
        "recommendation_reason": recommendation_reason(recommended),
    }


def comparison_key(run: dict[str, Any]) -> tuple[str, str, str, bool, float]:
    return (
        str(run.get("model") or ""),
        str(run.get("embedding_backend") or ""),
        str(run.get("embedding_model") or ""),
        bool(run.get("verify")),
        round(float(run.get("temperature") or 0), 4),
    )


def no_verifier_key(group: dict[str, Any]) -> tuple[str, str, str, bool, float]:
    return (
        str(group.get("model") or ""),
        str(group.get("embedding_backend") or ""),
        str(group.get("embedding_model") or ""),
        False,
        round(float(group.get("temperature") or 0), 4),
    )


def verifier_is_worthwhile(
    group: dict[str, Any],
    groups_by_key: dict[tuple[str, str, str, bool, float], dict[str, Any]],
) -> bool:
    if not group.get("verify"):
        return False
    counterpart = groups_by_key.get(no_verifier_key(group))
    if counterpart is None:
        return True
    total = max(int(group.get("total") or 0), int(counterpart.get("total") or 0), 1)
    pass_delta = int(group.get("passed") or 0) - int(counterpart.get("passed") or 0)
    meaningful_delta = max(1, math.ceil(total * 0.15))
    if pass_delta < meaningful_delta:
        return False
    verifier_latency = int(group.get("average_latency_ms") or 0)
    base_latency = max(int(counterpart.get("average_latency_ms") or 0), 1)
    return verifier_latency <= base_latency * 2 or pass_delta >= meaningful_delta + 1


def choose_recommended_group(
    groups: list[dict[str, Any]],
    groups_by_key: dict[tuple[str, str, str, bool, float], dict[str, Any]],
) -> dict[str, Any] | None:
    eligible = [
        group
        for group in groups
        if not group.get("verify") or verifier_is_worthwhile(group, groups_by_key)
    ]
    if not eligible:
        return None
    return sorted(
        eligible,
        key=lambda group: (
            -int(group.get("passed") or 0),
            int(group.get("average_latency_ms") or 0),
            bool(group.get("verify")),
            str(group.get("model") or ""),
        ),
    )[0]


def recommendation_reason(group: dict[str, Any] | None) -> str:
    if not group:
        return "No benchmark runs are available yet."
    verifier = "verifier on" if group.get("verify") else "verifier off"
    return (
        f"Recommended by highest pass count with lower latency as tie-breaker: "
        f"{group.get('model')} using {group.get('embedding_backend')}/{group.get('embedding_model')}, "
        f"{verifier}, temperature {float(group.get('temperature') or 0):.2f}."
    )


def guidance_labels_for_group(
    group: dict[str, Any],
    groups_by_key: dict[tuple[str, str, str, bool, float], dict[str, Any]],
) -> list[str]:
    labels: list[str] = []
    total = max(int(group.get("total") or 0), 1)
    passed = int(group.get("passed") or 0)
    pass_rate = passed / total
    expected_failures = int(group.get("expected_failures") or 0)
    forbidden_failures = int(group.get("forbidden_failures") or 0)
    source_failures = int(group.get("source_failures") or 0)

    if pass_rate >= 0.85 and source_failures == 0 and forbidden_failures == 0:
        labels.append("Recommended for Potato Mode")
    if pass_rate >= 0.65 and forbidden_failures == 0:
        labels.append("Good for direct extraction")
    if expected_failures > 0:
        labels.append("Weak at chronology")
    if source_failures > 0:
        labels.append("Source contamination risk")
    if forbidden_failures > 0 or pass_rate < 0.5:
        labels.append("Not recommended for evidence-sensitive answers")
    if group.get("verify"):
        if verifier_is_worthwhile(group, groups_by_key):
            labels.append("Verifier helped grounding")
        else:
            labels.append("Verifier not useful here")
    return dedupe_labels(labels)


def dedupe_labels(labels: list[str]) -> list[str]:
    seen: set[str] = set()
    result: list[str] = []
    for label in labels:
        if label in seen:
            continue
        seen.add(label)
        result.append(label)
    return result


def format_embedding_label(run: dict[str, Any]) -> str:
    backend = str(run.get("embedding_backend") or "")
    model = str(run.get("embedding_model") or "")
    if backend and model:
        return f"{backend}/{model}"
    return model or backend or "unknown"


def normalize(text: str) -> str:
    return " ".join(
        (text or "").lower().replace("\u2013", "-").replace("\u00e2\u20ac\u201c", "-").split()
    )


def phrase_matches(answer: str, phrase: str, *, mode: str) -> bool:
    normalized_phrase = normalize(phrase)
    if not normalized_phrase:
        return False
    if normalized_phrase in answer:
        return True

    answer_tokens = fact_tokens(answer)
    phrase_tokens = fact_tokens(normalized_phrase)
    if not phrase_tokens:
        return False

    overlap = len(answer_tokens & phrase_tokens) / len(phrase_tokens)
    if mode == "forbidden":
        if len(phrase_tokens) <= 2:
            return phrase_tokens.issubset(answer_tokens)
        return overlap >= 0.86

    threshold = 1.0 if len(phrase_tokens) <= 2 else 0.62
    return overlap >= threshold


def fact_tokens(text: str) -> set[str]:
    tokens = {stem(token.lower()) for token in WORD_RE.findall(text or "")}
    important = {token for token in tokens if token and token not in FACT_STOPWORDS}
    return important or tokens


def stem(token: str) -> str:
    if token in {"came", "coming"}:
        return "come"
    if token in {"told", "telling"}:
        return "tell"
    if token in {"left", "leaving"}:
        return "leave"
    for suffix in ("ing", "ed", "es", "s"):
        if len(token) > len(suffix) + 3 and token.endswith(suffix):
            return token[: -len(suffix)]
    return token


def retrieved_document_ids(retrieved_chunks: list[dict[str, Any]]) -> list[str]:
    seen: set[str] = set()
    ids: list[str] = []
    for chunk in retrieved_chunks:
        document_id = str(chunk.get("document_id") or "")
        if document_id and document_id not in seen:
            seen.add(document_id)
            ids.append(document_id)
    return ids


def retrieved_chunk_ids(retrieved_chunks: list[dict[str, Any]]) -> list[str]:
    return [
        str(chunk.get("chunk_id") or "")
        for chunk in retrieved_chunks
        if chunk.get("chunk_id")
    ]


def safe_name(value: str) -> str:
    return "".join(char if char.isalnum() else "-" for char in value).strip("-") or "model"
