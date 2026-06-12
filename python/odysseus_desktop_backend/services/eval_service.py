from __future__ import annotations

import json
import tempfile
import time
import uuid
import math
import statistics
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
        return benchmark_comparison(runs, current_suite_version=EVAL_SUITE_VERSION)

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


def benchmark_comparison(
    runs: list[dict[str, Any]],
    *,
    current_suite_version: str = EVAL_SUITE_VERSION,
) -> dict[str, Any]:
    comparable_runs = []
    excluded_runs = []
    for run in runs:
        if str(run.get("suite_version") or "") == current_suite_version:
            comparable_runs.append(run)
        else:
            excluded_runs.append(run)
    excluded_suite_versions = sorted(
        {
            str(run.get("suite_version") or "unknown")
            for run in excluded_runs
        }
    )
    grouped_runs: dict[tuple[str, str, str, bool, float], list[dict[str, Any]]] = {}
    for run in comparable_runs:
        grouped_runs.setdefault(comparison_key(run), []).append(run)

    groups_by_key: dict[tuple[str, str, str, bool, float], dict[str, Any]] = {}
    for key, config_runs in grouped_runs.items():
        sorted_runs = sorted(config_runs, key=run_created_at, reverse=True)
        latest_run = sorted_runs[0]
        best_run = sorted(
            sorted_runs,
            key=lambda run: (-run_passed_count(run), run_average_latency_ms(run), -run_created_at(run)),
        )[0]
        latest_cases = run_cases(latest_run)
        latest_expected_failures, latest_forbidden_failures, latest_source_failures = case_failure_counts(latest_cases)
        run_passed_values = [run_passed_count(run) for run in sorted_runs]
        run_pass_rates = [run_pass_rate(run) for run in sorted_runs]
        run_latencies = [run_average_latency_ms(run) for run in sorted_runs]
        run_count = len(sorted_runs)
        total_runtime_ms = sum(int(run.get("total_runtime_ms") or 0) for run in sorted_runs)
        latest_total = run_total_count(latest_run)
        best_total = run_total_count(best_run)

        group = {
            "key": "|".join(str(item) for item in key),
            "model": str(latest_run.get("model") or ""),
            "embedding_backend": str(latest_run.get("embedding_backend") or ""),
            "embedding_model": str(latest_run.get("embedding_model") or ""),
            "verify": bool(latest_run.get("verify")),
            "temperature": float(latest_run.get("temperature") or 0),
            "suite_version": str(latest_run.get("suite_version") or ""),
            "run_count": run_count,
            "latest_run_passed": run_passed_count(latest_run),
            "latest_run_total": latest_total,
            "latest_run_pass_rate": run_pass_rate(latest_run),
            "latest_run_avg_latency_ms": run_average_latency_ms(latest_run),
            "latest_expected_failures": latest_expected_failures,
            "latest_forbidden_failures": latest_forbidden_failures,
            "latest_source_failures": latest_source_failures,
            "latest_created_at": run_created_at(latest_run),
            "best_run_passed": run_passed_count(best_run),
            "best_run_total": best_total,
            "best_run_pass_rate": run_pass_rate(best_run),
            "best_run_avg_latency_ms": run_average_latency_ms(best_run),
            "best_created_at": run_created_at(best_run),
            "mean_passed_per_run": sum(run_passed_values) / run_count if run_count else 0.0,
            "mean_pass_rate": sum(run_pass_rates) / run_count if run_count else 0.0,
            "median_avg_latency_ms": int(statistics.median(run_latencies)) if run_latencies else 0,
            "mean_avg_latency_ms": int(statistics.mean(run_latencies)) if run_latencies else 0,
            "total_runtime_ms": total_runtime_ms,
            "cumulative_passed": sum(run_passed_values),
            "cumulative_total": sum(run_total_count(run) for run in sorted_runs),
        }

        # Backward-compatible aliases for older callers; these now represent the
        # latest run or median latency, not cumulative benchmark quality.
        group.update(
            {
                "passed": group["latest_run_passed"],
                "total": group["latest_run_total"],
                "expected_failures": group["latest_expected_failures"],
                "forbidden_failures": group["latest_forbidden_failures"],
                "source_failures": group["latest_source_failures"],
                "average_latency_ms": group["median_avg_latency_ms"],
                "pass_rate": group["latest_run_pass_rate"],
            }
        )
        groups_by_key[key] = group

    groups = []
    for group in groups_by_key.values():
        group["guidance_labels"] = guidance_labels_for_group(group, groups_by_key)
        group["verifier_recommended"] = verifier_is_worthwhile(group, groups_by_key)
        groups.append(group)
    add_fastest_usable_label(groups)

    groups.sort(
        key=lambda group: (
            -recommendation_score(group),
            int(group["latest_run_avg_latency_ms"] or group["median_avg_latency_ms"]),
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
        "recommendation_reason": recommendation_reason(
            recommended,
            current_suite_version=current_suite_version,
            excluded_count=len(excluded_runs),
        ),
        "case_difficulty": case_difficulty_summary(comparable_runs),
        "comparison_suite_version": current_suite_version,
        "included_run_count": len(comparable_runs),
        "excluded_run_count": len(excluded_runs),
        "excluded_suite_versions": excluded_suite_versions,
    }


def run_cases(run: dict[str, Any]) -> list[dict[str, Any]]:
    cases = run.get("cases")
    return cases if isinstance(cases, list) else []


def run_passed_count(run: dict[str, Any]) -> int:
    return int(run.get("total_passed") or 0)


def run_total_count(run: dict[str, Any]) -> int:
    cases = run_cases(run)
    if cases:
        return len(cases)
    return run_passed_count(run) + int(run.get("total_failed") or 0)


def run_pass_rate(run: dict[str, Any]) -> float:
    total = run_total_count(run)
    return float(run_passed_count(run)) / total if total else 0.0


def run_average_latency_ms(run: dict[str, Any]) -> int:
    latency = run.get("average_latency_ms")
    if latency is not None:
        return int(latency or 0)
    cases = run_cases(run)
    if not cases:
        return 0
    return int(sum(int(case.get("latency_ms") or 0) for case in cases) / len(cases))


def run_created_at(run: dict[str, Any]) -> int:
    return int(run.get("created_at") or 0)


def case_failure_counts(cases: list[dict[str, Any]]) -> tuple[int, int, int]:
    expected_failures = sum(1 for case in cases if not case.get("expected_passed"))
    forbidden_failures = sum(1 for case in cases if not case.get("forbidden_passed"))
    source_failures = sum(1 for case in cases if not case.get("source_passed"))
    return expected_failures, forbidden_failures, source_failures


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
    total = max(int(group.get("latest_run_total") or 0), int(counterpart.get("latest_run_total") or 0), 1)
    pass_delta = int(group.get("latest_run_passed") or 0) - int(counterpart.get("latest_run_passed") or 0)
    meaningful_delta = max(1, math.ceil(total * 0.15))
    if pass_delta < meaningful_delta:
        return False
    verifier_latency = int(group.get("latest_run_avg_latency_ms") or group.get("median_avg_latency_ms") or 0)
    base_latency = max(int(counterpart.get("latest_run_avg_latency_ms") or counterpart.get("median_avg_latency_ms") or 0), 1)
    return verifier_latency <= base_latency * 2


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
            -recommendation_score(group),
            int(group.get("latest_run_avg_latency_ms") or group.get("median_avg_latency_ms") or 0),
            bool(group.get("verify")),
            str(group.get("model") or ""),
        ),
    )[0]


def recommendation_score(group: dict[str, Any]) -> float:
    if int(group.get("run_count") or 0) > 1:
        return float(group.get("mean_pass_rate") or 0.0)
    return float(group.get("latest_run_pass_rate") or 0.0)


def recommendation_reason(
    group: dict[str, Any] | None,
    *,
    current_suite_version: str = EVAL_SUITE_VERSION,
    excluded_count: int = 0,
) -> str:
    if not group:
        if excluded_count:
            return (
                f"No comparable {current_suite_version} benchmark runs yet. "
                f"{excluded_count} older/incompatible run(s) are excluded from recommendation."
            )
        return "No benchmark runs are available yet."
    verifier = "verifier on" if group.get("verify") else "verifier off"
    excluded_note = (
        f" {excluded_count} older/incompatible run(s) are excluded."
        if excluded_count
        else ""
    )
    return (
        f"Recommended among {current_suite_version} runs by latest/mean pass rate with lower latency as tie-breaker: "
        f"{group.get('model')} using {group.get('embedding_backend')}/{group.get('embedding_model')}, "
        f"{verifier}, temperature {float(group.get('temperature') or 0):.2f}."
        f"{excluded_note}"
    )


def guidance_labels_for_group(
    group: dict[str, Any],
    groups_by_key: dict[tuple[str, str, str, bool, float], dict[str, Any]],
) -> list[str]:
    labels: list[str] = []
    pass_rate = float(group.get("mean_pass_rate") or group.get("latest_run_pass_rate") or 0.0)
    expected_failures = int(group.get("latest_expected_failures") or 0)
    forbidden_failures = int(group.get("latest_forbidden_failures") or 0)
    source_failures = int(group.get("latest_source_failures") or 0)

    if pass_rate >= 0.85 and source_failures == 0 and forbidden_failures == 0:
        labels.append("Recommended for Potato Mode")
    if pass_rate >= 0.4 and forbidden_failures == 0:
        labels.append("Good extraction baseline")
    if expected_failures > 0:
        labels.append("Weak at chronology")
    if source_failures > 0:
        labels.append("Source contamination risk")
    if forbidden_failures > 0 or pass_rate < 0.5:
        labels.append("Not evidence-safe")
    if group.get("verify"):
        counterpart = groups_by_key.get(no_verifier_key(group))
        improved = counterpart is None or int(group.get("latest_run_passed") or 0) > int(counterpart.get("latest_run_passed") or 0)
        if verifier_is_worthwhile(group, groups_by_key):
            labels.append("Verifier improved score")
        elif improved:
            labels.append("Verifier improved score but high latency")
        else:
            labels.append("Verifier not worth latency")
    return dedupe_labels(labels)


def add_fastest_usable_label(groups: list[dict[str, Any]]) -> None:
    usable = [group for group in groups if int(group.get("latest_run_passed") or 0) > 0]
    if not usable:
        return
    best_passed = max(int(group.get("latest_run_passed") or 0) for group in usable)
    fastest = sorted(
        (group for group in usable if int(group.get("latest_run_passed") or 0) == best_passed),
        key=lambda group: (
            int(group.get("latest_run_avg_latency_ms") or group.get("median_avg_latency_ms") or 0),
            bool(group.get("verify")),
            str(group.get("model") or ""),
        ),
    )[0]
    fastest["guidance_labels"] = dedupe_labels(["Fastest usable config", *fastest.get("guidance_labels", [])])


def case_difficulty_summary(runs: list[dict[str, Any]]) -> dict[str, list[dict[str, Any]]]:
    stats: dict[str, dict[str, Any]] = {}
    for run in runs:
        for case in run_cases(run):
            case_id = str(case.get("case_id") or "unknown")
            entry = stats.setdefault(
                case_id,
                {
                    "case_id": case_id,
                    "question": str(case.get("question") or ""),
                    "required_source_document": str(case.get("required_source_document") or ""),
                    "attempts": 0,
                    "passes": 0,
                    "source_failures": 0,
                    "forbidden_failures": 0,
                },
            )
            entry["attempts"] += 1
            if case.get("passed"):
                entry["passes"] += 1
            if not case.get("source_passed"):
                entry["source_failures"] += 1
            if not case.get("forbidden_passed"):
                entry["forbidden_failures"] += 1

    items = [case_difficulty_item(entry) for entry in stats.values()]
    usually_pass = sorted(
        [item for item in items if item["pass_rate"] >= 0.75],
        key=lambda item: (-float(item["pass_rate"]), -int(item["attempts"]), str(item["case_id"])),
    )
    usually_fail = sorted(
        [item for item in items if item["pass_rate"] <= 0.4],
        key=lambda item: (float(item["pass_rate"]), -int(item["attempts"]), str(item["case_id"])),
    )
    frequent_source_failures = sorted(
        [item for item in items if item["source_failure_rate"] >= 0.4 and int(item["source_failures"]) > 0],
        key=lambda item: (-float(item["source_failure_rate"]), -int(item["attempts"]), str(item["case_id"])),
    )
    frequent_forbidden_failures = sorted(
        [item for item in items if item["forbidden_failure_rate"] >= 0.4 and int(item["forbidden_failures"]) > 0],
        key=lambda item: (-float(item["forbidden_failure_rate"]), -int(item["attempts"]), str(item["case_id"])),
    )
    return {
        "usually_pass": usually_pass,
        "usually_fail": usually_fail,
        "frequent_source_failures": frequent_source_failures,
        "frequent_forbidden_failures": frequent_forbidden_failures,
    }


def case_difficulty_item(entry: dict[str, Any]) -> dict[str, Any]:
    attempts = max(int(entry.get("attempts") or 0), 1)
    passes = int(entry.get("passes") or 0)
    source_failures = int(entry.get("source_failures") or 0)
    forbidden_failures = int(entry.get("forbidden_failures") or 0)
    return {
        "case_id": str(entry.get("case_id") or ""),
        "question": str(entry.get("question") or ""),
        "required_source_document": str(entry.get("required_source_document") or ""),
        "attempts": attempts,
        "passes": passes,
        "failures": attempts - passes,
        "pass_rate": passes / attempts,
        "source_failures": source_failures,
        "source_failure_rate": source_failures / attempts,
        "forbidden_failures": forbidden_failures,
        "forbidden_failure_rate": forbidden_failures / attempts,
    }


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
