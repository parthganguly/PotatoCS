from __future__ import annotations

import json
import os
import tempfile
import time
import uuid
import math
import statistics
from collections.abc import Callable
from pathlib import Path
from typing import Any
import re

from odysseus_desktop_backend import __version__
from odysseus_desktop_backend.logging_config import get_logger
from odysseus_desktop_backend.services.chat_service import (
    ChatService,
    DEFAULT_TEMPERATURE,
    format_snippets_for_verifier,
)
from odysseus_desktop_backend.services.document_service import DocumentService
from odysseus_desktop_backend.services.embedding_service import DEFAULT_EMBEDDING_MODEL, EmbeddingService
from odysseus_desktop_backend.services.model_service import (
    BENCHMARK_ANSWER_TIMEOUT_SECONDS,
    CORRECTION_TIMEOUT_SECONDS,
    VERIFIER_TIMEOUT_SECONDS,
    ModelService,
    ModelServiceError,
    ModelTimeoutError,
    normalize_thinking_mode,
)
from odysseus_desktop_backend.services.rag_service import RAGService
from odysseus_desktop_backend.services.session_service import SessionService
from odysseus_desktop_backend.services.settings_service import SettingsService
from odysseus_desktop_backend.services.vector_store import SQLiteNumPyVectorStore
from odysseus_desktop_backend.storage import Database, utc_ms


EVAL_SUITE_NAME = "local-rag"
EVAL_SUITE_VERSION = "v0.1.12"
PROMPT_VERSION = "rag-benchmark-v0.1.12"
BENCHMARK_MODES = {"retrieval_only", "oracle_generation", "end_to_end"}
DEFAULT_BENCHMARK_MODE = "end_to_end"
DEFAULT_THINKING_MODE = "off"
WORD_RE = re.compile(r"[a-z0-9]+", re.IGNORECASE)
CODE_RE = re.compile(r"\b[A-Z]+-\d+[A-Z0-9-]*\b", re.IGNORECASE)
TIME_RE = re.compile(r"\b\d{1,2}:\d{2}\b")
NUMBER_RE = re.compile(r"\b\d+(?:\.\d+)?\b")
NEGATION_WORDS = {"no", "not", "never", "without", "cannot", "can't", "doesn't", "dont", "don't", "didn't"}
NUMBER_WORDS = {
    "zero": "0",
    "one": "1",
    "two": "2",
    "three": "3",
    "four": "4",
    "five": "5",
    "six": "6",
    "seven": "7",
    "eight": "8",
    "nine": "9",
    "ten": "10",
}
ABSENCE_TYPES = {"abstention", "absence_or_abstention"}
NEGATIVE_TYPES = {"negative", "negative_fact"}
POSITIVE_TYPES = {"positive", "positive_fact"}
EXACT_TYPES = {"exact_identifier", "quantity", "date", "date_or_time", "code"}
RELATION_TYPES = {"relation"}
PIPELINE_BUCKETS = ("passed", "retrieval_only", "generation_only", "both", "grader_review", "timeout", "runtime_error")
GUIDANCE_LABEL_ORDER = (
    "Recommended",
    "Fastest usable config",
    "Best quality config",
    "Provisional",
    "Preliminary",
    "Benchmarked",
    "Recommended for Potato Mode",
    "Good extraction baseline",
    "Weak at chronology",
    "Source contamination risk",
    "Not evidence-safe",
    "Verifier not worth latency",
)
ABSENCE_VERBS = (
    "identify",
    "list",
    "state",
    "specify",
    "mention",
    "provide",
    "name",
    "establish",
    "confirm",
    "say",
    "include",
)
ABSENCE_REGEXES = [
    re.compile(r"\b(?:does|do|did)\s+not\s+(?:identify|list|state|specify|mention|provide|name|establish|confirm|say|include)\b", re.IGNORECASE),
    re.compile(r"\b(?:is|are|was|were)\s+not\s+(?:identified|listed|stated|specified|mentioned|provided|named|established|confirmed|available)\b", re.IGNORECASE),
    re.compile(r"\bno\s+[^,;.!?]{0,90}\b(?:is|are|was|were)?\s*(?:listed|identified|provided|available|mentioned|stated|specified|named|confirmed|established)\b", re.IGNORECASE),
    re.compile(r"\bno\s+information\s+(?:is\s+)?provided\b", re.IGNORECASE),
    re.compile(r"\b(?:context|document|specification|report|notice|evidence)\s+(?:is\s+)?silent\s+on\b", re.IGNORECASE),
    re.compile(r"\bcannot\s+(?:determine|confirm|establish)\b", re.IGNORECASE),
    re.compile(r"\bcan\s+not\s+(?:determine|confirm|establish)\b", re.IGNORECASE),
    re.compile(r"\bnot\s+(?:stated|available|provided|listed|identified|mentioned|specified|confirmed|established)\b", re.IGNORECASE),
    re.compile(r"\bprovided\s+evidence\s+does\s+not\s+say\b", re.IGNORECASE),
]
CONTRAST_MARKERS = ("but", "although", "though", "however", "yet")
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

    def chat_detailed(
        self,
        model: str,
        messages: list[dict[str, str]],
        *,
        options: dict[str, Any] | None = None,
        thinking: str = "auto",
        timeout: float = BENCHMARK_ANSWER_TIMEOUT_SECONDS,
        response_format: str | None = None,
    ) -> dict[str, Any]:
        merged_options = {"temperature": self.temperature}
        if options:
            merged_options.update(options)
        return super().chat_detailed(
            model,
            messages,
            options=merged_options,
            thinking=thinking,
            timeout=timeout,
            response_format=response_format,
        )


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
        benchmark_mode: str = DEFAULT_BENCHMARK_MODE,
        thinking_mode: str = DEFAULT_THINKING_MODE,
        repeats: int = 1,
        num_predict: int = 0,
    ) -> dict[str, Any]:
        model_name = (model or "").strip()
        if not model_name:
            raise ValueError("model is required")
        mode = normalize_benchmark_mode(benchmark_mode)
        selected_thinking_mode = normalize_thinking_mode(thinking_mode)
        repeat_count = 3 if int(repeats or 1) == 3 else 1
        cases = [
            case
            for case in load_cases(self.cases_dir)
            if mode in benchmark_modes_for_case(case)
        ]
        if not cases:
            raise RuntimeError(f"No {mode} eval cases found in {self.cases_dir}")

        started = time.perf_counter()
        run = self._start_run(
            model=model_name,
            verify=verify,
            temperature=temperature,
            benchmark_mode=mode,
            thinking_mode=selected_thinking_mode,
            answer_style=answer_style_override or "",
            repeat_count=repeat_count,
            num_predict=num_predict,
        )
        logger.info(
            "benchmark run started run_id=%s model=%s mode=%s thinking=%s verify=%s cases=%s repeats=%s",
            run["id"],
            model_name,
            mode,
            selected_thinking_mode,
            verify,
            len(cases),
            repeat_count,
        )

        try:
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
                    for repeat_index in range(1, repeat_count + 1):
                        for case in cases:
                            missing_documents = missing_case_document_ids(case, document_ids)
                            if missing_documents:
                                case_result = error_case_result(
                                    case,
                                    mode=mode,
                                    thinking_mode=selected_thinking_mode,
                                    repeat_index=repeat_index,
                                    temperature=temperature,
                                    stage="indexing",
                                    error=(
                                        "required benchmark fixture(s) were not indexed: "
                                        + ", ".join(missing_documents)
                                    ),
                                )
                                self._store_case_result(run["id"], case_result)
                                continue
                            try:
                                case_result = self._run_case(
                                    case,
                                    mode=mode,
                                    model_name=model_name,
                                    verify=verify,
                                    answer_style_override=answer_style_override,
                                    temperature=temperature,
                                    thinking_mode=selected_thinking_mode,
                                    num_predict=num_predict,
                                    repeat_index=repeat_index,
                                    document_ids=document_ids,
                                    rag=rag,
                                    chat=chat,
                                    models=models,
                                )
                            except ModelTimeoutError as exc:
                                case_result = timeout_case_result(
                                    case,
                                    mode=mode,
                                    thinking_mode=selected_thinking_mode,
                                    repeat_index=repeat_index,
                                    temperature=temperature,
                                    stage="answer",
                                    error=str(exc),
                                )
                            except Exception as exc:  # noqa: BLE001 - one case must not abort the run
                                logger.warning(
                                    "benchmark case failed run_id=%s case_id=%s error=%s",
                                    run["id"],
                                    case.get("id"),
                                    exc,
                                )
                                case_result = error_case_result(
                                    case,
                                    mode=mode,
                                    thinking_mode=selected_thinking_mode,
                                    repeat_index=repeat_index,
                                    temperature=temperature,
                                    stage="runtime",
                                    error=str(exc),
                                )
                            self._store_case_result(run["id"], case_result)
                finally:
                    temp_db.close()
        except Exception as exc:  # noqa: BLE001 - preserve benchmark history for setup failures
            logger.exception("benchmark run aborted run_id=%s error=%s", run["id"], exc)
            self._store_unattempted_case_errors(
                run["id"],
                cases,
                mode=mode,
                thinking_mode=selected_thinking_mode,
                repeat_count=repeat_count,
                temperature=temperature,
                stage="setup",
                error=str(exc),
            )
            total_runtime_ms = int((time.perf_counter() - started) * 1000)
            run = self._finalize_run(
                run["id"],
                total_runtime_ms=total_runtime_ms,
                status="error",
                notes=f"Benchmark aborted before completion: {exc}",
            )
            logger.info(
                "benchmark run aborted run_id=%s model=%s runtime_ms=%s",
                run["id"],
                model_name,
                total_runtime_ms,
            )
            return run

        total_runtime_ms = int((time.perf_counter() - started) * 1000)
        run = self._finalize_run(run["id"], total_runtime_ms=total_runtime_ms)
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

    def _run_case(
        self,
        case: dict[str, Any],
        *,
        mode: str,
        model_name: str,
        verify: bool,
        answer_style_override: str | None,
        temperature: float,
        thinking_mode: str,
        num_predict: int,
        repeat_index: int,
        document_ids: dict[str, str],
        rag: RAGService,
        chat: ChatService,
        models: ModelService,
    ) -> dict[str, Any]:
        case_started = time.perf_counter()
        required_source = str(case["required_source_document"])
        required_document_id = document_ids[required_source]
        answer_style = str(answer_style_override or case.get("answer_style") or "precise")
        retrieval_audit = rag.search_with_audit(
            str(case["question"]),
            limit=5,
            document_ids=document_scope_for_case(case, document_ids),
        )
        retrieval_metrics = retrieval_metrics_for_case(case, retrieval_audit, required_document_id)

        if mode == "retrieval_only":
            elapsed_ms = int((time.perf_counter() - case_started) * 1000)
            passed = bool(retrieval_metrics.get("hit_at1"))
            return base_case_result(
                case,
                mode=mode,
                thinking_mode=thinking_mode,
                repeat_index=repeat_index,
                answer_style=answer_style,
                temperature=temperature,
                status="completed",
                passed=passed,
                expected_passed=True,
                forbidden_passed=True,
                source_passed=passed,
                latency_ms=elapsed_ms,
                reasons=[] if passed else ["required source was not ranked first"],
                retrieved_chunks=[search_result_for_ids(item) for item in retrieval_audit.get("results", [])],
                embedding_backend=str(retrieval_audit.get("embedding_backend") or ""),
                embedding_model=str(retrieval_audit.get("embedding_model") or ""),
                retrieval_metrics=retrieval_metrics,
                retrieval_candidates=list(retrieval_audit.get("candidates") or []),
                pipeline_diagnosis="passed" if passed else "retrieval_only",
            )

        if mode == "oracle_generation":
            prompt = oracle_prompt(case, gold_evidence_for_case(case))
            try:
                response = call_model_detailed(
                    models,
                    model_name,
                    [
                        {"role": "system", "content": oracle_system_prompt(answer_style)},
                        {"role": "user", "content": prompt},
                    ],
                    options_for_generation(temperature, num_predict),
                    thinking=thinking_mode,
                    timeout=BENCHMARK_ANSWER_TIMEOUT_SECONDS,
                )
            except ModelTimeoutError as exc:
                elapsed_ms = int((time.perf_counter() - case_started) * 1000)
                result = timeout_case_result(
                    case,
                    mode=mode,
                    thinking_mode=thinking_mode,
                    repeat_index=repeat_index,
                    temperature=temperature,
                    stage="answer",
                    error=str(exc),
                )
                result.update(
                    {
                        "latency_ms": elapsed_ms,
                        "retrieval_metrics": retrieval_metrics,
                        "retrieval_candidates": list(retrieval_audit.get("candidates") or []),
                        "embedding_backend": str(retrieval_audit.get("embedding_backend") or ""),
                        "embedding_model": str(retrieval_audit.get("embedding_model") or ""),
                        "prompt_text": prompt,
                    }
                )
                return result
            answer = str(response.get("content") or "")
            grading = evaluate_answer(
                case,
                answer,
                supplied_document_ids=[required_document_id],
                required_document_id=required_document_id,
            )
            elapsed_ms = int((time.perf_counter() - case_started) * 1000)
            return base_case_result(
                case,
                mode=mode,
                thinking_mode=thinking_mode,
                repeat_index=repeat_index,
                answer_style=answer_style,
                temperature=temperature,
                status="completed",
                passed=bool(grading["passed"]),
                expected_passed=bool(grading["expected_passed"]),
                forbidden_passed=bool(grading["forbidden_passed"]),
                source_passed=True,
                latency_ms=elapsed_ms,
                reasons=list(grading["reasons"]),
                retrieved_chunks=[],
                embedding_backend=str(retrieval_audit.get("embedding_backend") or ""),
                embedding_model=str(retrieval_audit.get("embedding_model") or ""),
                retrieval_metrics=retrieval_metrics,
                retrieval_candidates=list(retrieval_audit.get("candidates") or []),
                supplied_evidence=[{"document_id": required_document_id, "text": gold_evidence_for_case(case)}],
                prompt_text=prompt,
                answer_content=answer,
                thinking_text=str(response.get("thinking") or ""),
                model_response=response,
                grader_matches=list(grading["matches"]),
                grader_review_required=bool(grading["grader_review_required"]),
                pipeline_diagnosis=diagnose_generation_only(grading),
            )

        try:
            result = chat.send(
                str(case["question"]),
                model=model_name,
                use_rag=True,
                document_ids=document_scope_for_case(case, document_ids),
                verify_rag=verify,
                answer_style=answer_style,
                temperature=temperature,
                thinking_mode=thinking_mode,
                timeout=BENCHMARK_ANSWER_TIMEOUT_SECONDS,
            )
        except ModelTimeoutError as exc:
            elapsed_ms = int((time.perf_counter() - case_started) * 1000)
            timeout_result = timeout_case_result(
                case,
                mode=mode,
                thinking_mode=thinking_mode,
                repeat_index=repeat_index,
                temperature=temperature,
                stage="answer",
                error=str(exc),
            )
            timeout_result.update(
                {
                    "latency_ms": elapsed_ms,
                    "retrieved_document_ids": list(retrieval_metrics.get("ranked_retrieved_document_ids") or []),
                    "retrieved_chunk_ids": list(retrieval_metrics.get("ranked_retrieved_chunk_ids") or []),
                    "embedding_backend": str(retrieval_audit.get("embedding_backend") or ""),
                    "embedding_model": str(retrieval_audit.get("embedding_model") or ""),
                    "retrieval_metrics": retrieval_metrics,
                    "retrieval_candidates": list(retrieval_audit.get("candidates") or []),
                }
            )
            return timeout_result
        elapsed_ms = int((time.perf_counter() - case_started) * 1000)
        retrieved_chunks = result.get("retrieved_chunks") or []
        retrieved_snippets = result.get("retrieved_snippets") or []
        supplied_document_ids = retrieved_document_ids(retrieved_snippets)
        answer = str(result.get("assistant_message", {}).get("content") or "")
        grading = evaluate_answer(
            case,
            answer,
            supplied_document_ids=supplied_document_ids,
            required_document_id=required_document_id,
        )
        model_response = result.get("model_response") or {}
        grounding = result.get("grounding") or {}
        verifier = grounding.get("verifier") if isinstance(grounding.get("verifier"), dict) else {}
        if verifier.get("status") == "timeout":
            grading["reasons"].append("verifier timed out")
        case_result = base_case_result(
            case,
            mode=mode,
            thinking_mode=thinking_mode,
            repeat_index=repeat_index,
            answer_style=answer_style,
            temperature=temperature,
            status="completed",
            passed=bool(grading["passed"]),
            expected_passed=bool(grading["expected_passed"]),
            forbidden_passed=bool(grading["forbidden_passed"]),
            source_passed=bool(grading["source_passed"]),
            latency_ms=elapsed_ms,
            reasons=list(grading["reasons"]),
            retrieved_chunks=retrieved_chunks,
            embedding_backend=str((rag.health().get("embedding") or {}).get("backend") or ""),
            embedding_model=str((rag.health().get("embedding") or {}).get("model") or ""),
            retrieval_metrics=retrieval_metrics,
            retrieval_candidates=list(retrieval_audit.get("candidates") or []),
            supplied_evidence=retrieved_snippets,
            prompt_text=benchmark_prompt_snapshot(str(case["question"]), retrieved_snippets, answer_style),
            answer_content=answer,
            thinking_text=str(model_response.get("thinking") or ""),
            model_response=model_response,
            grader_matches=list(grading["matches"]),
            grader_review_required=bool(grading["grader_review_required"]),
            timings=dict(result.get("timings") or {}),
            pipeline_diagnosis=diagnose_end_to_end(retrieval_metrics, grading, verifier),
        )
        if verifier.get("status"):
            case_result["verifier_status"] = str(verifier.get("status"))
        return case_result

    def _start_run(
        self,
        *,
        model: str,
        verify: bool,
        temperature: float,
        benchmark_mode: str,
        thinking_mode: str,
        answer_style: str,
        repeat_count: int,
        num_predict: int,
    ) -> dict[str, Any]:
        now = utc_ms()
        run_id = str(uuid.uuid4())
        model_info = self._model_preflight(model)
        self.db.conn.execute(
            """
            INSERT INTO benchmark_runs(
                id, model, verify, suite_name, suite_version, total_passed,
                total_failed, average_latency_ms, total_runtime_ms, notes,
                embedding_backend, embedding_model, temperature, created_at,
                app_version, prompt_version, benchmark_mode, thinking_mode,
                answer_style, status, repeat_count, num_predict,
                timeout_policy_json, model_info_json
            )
            VALUES (?, ?, ?, ?, ?, 0, 0, 0, 0, '', '', '', ?, ?, ?, ?, ?, ?, ?, 'running', ?, ?, ?, ?)
            """,
            (
                run_id,
                model,
                1 if verify else 0,
                EVAL_SUITE_NAME,
                EVAL_SUITE_VERSION,
                float(temperature),
                now,
                __version__,
                PROMPT_VERSION,
                benchmark_mode,
                thinking_mode,
                answer_style,
                repeat_count,
                int(num_predict or 0),
                json.dumps(timeout_policy()),
                json.dumps(model_info),
            ),
        )
        self.db.conn.commit()
        row = self.db.conn.execute("SELECT * FROM benchmark_runs WHERE id = ?", (run_id,)).fetchone()
        if row is None:
            raise RuntimeError("benchmark run was not started")
        return self._run_with_cases(row)

    def _store_case_result(self, run_id: str, case: dict[str, Any]) -> None:
        now = utc_ms()
        diagnosis = canonical_case_diagnosis(case)
        self.db.conn.execute(
            """
            INSERT INTO benchmark_case_results(
                id, run_id, case_id, question, answer_style, required_source_document,
                passed, expected_passed, forbidden_passed, source_passed, latency_ms,
                reasons_json, retrieved_document_ids_json, retrieved_chunk_ids_json,
                embedding_backend, embedding_model, temperature, created_at,
                case_category, case_difficulty, benchmark_mode, thinking_mode, repeat_index,
                status, stage, pipeline_diagnosis, counts_toward_primary,
                grader_review_required, answer_content, thinking_text, thinking_returned,
                thinking_char_count, prompt_text, corrected_answer, model_response_json,
                retrieval_metrics_json, retrieval_candidates_json, supplied_evidence_json,
                grader_matches_json, timings_json, error_message
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
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
                json.dumps(case.get("reasons") or []),
                json.dumps(case.get("retrieved_document_ids") or []),
                json.dumps(case.get("retrieved_chunk_ids") or []),
                str(case.get("embedding_backend") or ""),
                str(case.get("embedding_model") or ""),
                float(case.get("temperature") or 0),
                now,
                str(case.get("case_category") or ""),
                str(case.get("case_difficulty") or ""),
                str(case.get("benchmark_mode") or DEFAULT_BENCHMARK_MODE),
                str(case.get("thinking_mode") or "legacy/unrecorded"),
                int(case.get("repeat_index") or 1),
                str(case.get("status") or "completed"),
                str(case.get("stage") or ""),
                diagnosis,
                1 if case.get("counts_toward_primary") else 0,
                1 if case.get("grader_review_required") else 0,
                str(case.get("answer_content") or ""),
                str(case.get("thinking_text") or ""),
                1 if case.get("thinking_text") else 0,
                len(str(case.get("thinking_text") or "")),
                str(case.get("prompt_text") or ""),
                str(case.get("corrected_answer") or ""),
                json.dumps(case.get("model_response") or {}),
                json.dumps(case.get("retrieval_metrics") or {}),
                json.dumps(case.get("retrieval_candidates") or []),
                json.dumps(case.get("supplied_evidence") or []),
                json.dumps(case.get("grader_matches") or []),
                json.dumps(case.get("timings") or {}),
                str(case.get("error_message") or ""),
            ),
        )
        self.db.conn.commit()

    def _store_unattempted_case_errors(
        self,
        run_id: str,
        cases: list[dict[str, Any]],
        *,
        mode: str,
        thinking_mode: str,
        repeat_count: int,
        temperature: float,
        stage: str,
        error: str,
    ) -> None:
        rows = self.db.conn.execute(
            """
            SELECT case_id, repeat_index
            FROM benchmark_case_results
            WHERE run_id = ?
            """,
            (run_id,),
        ).fetchall()
        existing = {
            (str(row["case_id"]), int(row["repeat_index"] or 1))
            for row in rows
        }
        for repeat_index in range(1, repeat_count + 1):
            for case in cases:
                case_id = str(case.get("id") or "")
                if (case_id, repeat_index) in existing:
                    continue
                self._store_case_result(
                    run_id,
                    error_case_result(
                        case,
                        mode=mode,
                        thinking_mode=thinking_mode,
                        repeat_index=repeat_index,
                        temperature=temperature,
                        stage=stage,
                        error=error,
                    ),
                )

    def _finalize_run(
        self,
        run_id: str,
        *,
        total_runtime_ms: int,
        status: str = "completed",
        notes: str | None = None,
    ) -> dict[str, Any]:
        rows = self.db.conn.execute(
            "SELECT * FROM benchmark_case_results WHERE run_id = ?",
            (run_id,),
        ).fetchall()
        cases = [benchmark_case_dict(row) for row in rows]
        total_passed = sum(1 for case in cases if case.get("passed"))
        total_failed = sum(1 for case in cases if primary_case_bucket(case) in {"retrieval_only", "generation_only", "both"})
        latencies = [int(case.get("latency_ms") or 0) for case in cases]
        average_latency_ms = int(sum(latencies) / len(latencies)) if latencies else 0
        embedding_backend = str(cases[0].get("embedding_backend") or "") if cases else ""
        embedding_model = str(cases[0].get("embedding_model") or "") if cases else ""
        timeout_count = sum(1 for case in cases if case.get("status") == "timeout")
        runtime_error_count = sum(1 for case in cases if case.get("status") == "error")
        grader_review_count = sum(1 for case in cases if case.get("grader_review_required"))
        scores = score_run_cases(cases)
        row = self.db.conn.execute("SELECT model, model_info_json FROM benchmark_runs WHERE id = ?", (run_id,)).fetchone()
        model_info = json_loads(row_get(row, "model_info_json", "{}"), {}) if row is not None else {}
        observed_model_info = self._observed_model_info(str(row_get(row, "model", ""))) if row is not None else {}
        if observed_model_info:
            model_info["observed_after_run"] = observed_model_info
        self.db.conn.execute(
            """
            UPDATE benchmark_runs
            SET total_passed = ?,
                total_failed = ?,
                average_latency_ms = ?,
                total_runtime_ms = ?,
                embedding_backend = ?,
                embedding_model = ?,
                notes = COALESCE(?, notes),
                status = ?,
                timeout_count = ?,
                runtime_error_count = ?,
                grader_review_count = ?,
                retrieval_score_json = ?,
                oracle_score_json = ?,
                end_to_end_score_json = ?,
                practical_score_json = ?,
                adversarial_score_json = ?,
                model_info_json = ?,
                completed_at = ?
            WHERE id = ?
            """,
            (
                total_passed,
                total_failed,
                average_latency_ms,
                total_runtime_ms,
                embedding_backend,
                embedding_model,
                notes,
                status,
                timeout_count,
                runtime_error_count,
                grader_review_count,
                json.dumps(scores["retrieval"]),
                json.dumps(scores["oracle_generation"]),
                json.dumps(scores["end_to_end"]),
                json.dumps(scores["practical"]),
                json.dumps(scores["adversarial"]),
                json.dumps(model_info),
                utc_ms(),
                run_id,
            ),
        )
        self.db.conn.commit()
        row = self.db.conn.execute("SELECT * FROM benchmark_runs WHERE id = ?", (run_id,)).fetchone()
        if row is None:
            raise RuntimeError("benchmark run was not finalized")
        return self._run_with_cases(row)

    def recover_interrupted_runs(self, *, reason: str) -> int:
        rows = self.db.conn.execute(
            """
            SELECT id, created_at, total_runtime_ms, notes
            FROM benchmark_runs
            WHERE status = 'running'
            """
        ).fetchall()
        if not rows:
            return 0
        now = utc_ms()
        for row in rows:
            elapsed_ms = max(int(row["total_runtime_ms"] or 0), now - int(row["created_at"] or now))
            existing_notes = str(row["notes"] or "").strip()
            note = f"{existing_notes}\n{reason}".strip() if existing_notes else reason
            self.db.conn.execute(
                """
                UPDATE benchmark_runs
                SET status = 'interrupted',
                    notes = ?,
                    total_runtime_ms = ?,
                    runtime_error_count = CASE
                        WHEN runtime_error_count > 0 THEN runtime_error_count
                        ELSE 1
                    END,
                    completed_at = ?
                WHERE id = ?
                """,
                (note, elapsed_ms, now, row["id"]),
            )
        self.db.conn.commit()
        return len(rows)

    def _model_preflight(self, model_name: str) -> dict[str, Any]:
        try:
            status = ModelService(self.db).ps()
        except Exception as exc:  # noqa: BLE001 - diagnostics must not block benchmark
            return {"error": str(exc), "warning": ""}
        for model in status.get("models", []):
            if str(model.get("name") or model.get("model") or "") == model_name:
                warning = ""
                if model.get("partially_cpu_offloaded"):
                    warning = (
                        "This model is partially CPU-offloaded and may be slow. "
                        "Verifier mode may multiply runtime on this configuration."
                    )
                return {**model, "already_loaded": True, "warning": warning}
        return {"already_loaded": False, "warning": "", "ps": status}

    def _observed_model_info(self, model_name: str) -> dict[str, Any]:
        if not model_name:
            return {}
        try:
            status = ModelService(self.db).ps()
        except Exception as exc:  # noqa: BLE001
            return {"error": str(exc)}
        for model in status.get("models", []):
            if str(model.get("name") or model.get("model") or "") == model_name:
                return dict(model)
        return {}

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
    return backend_root / "evals" / "rag_cases_v018"


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
            try:
                logger.info(
                    "benchmark indexing fixture fixture_id=%s path=%s",
                    fixture_id,
                    path,
                )
                imported = documents.import_document(str(path))
                result = rag.index_document(imported["id"])
                indexed[fixture_id] = imported["id"]
                logger.info(
                    "benchmark indexed fixture fixture_id=%s document_id=%s chunks=%s embedded=%s cached=%s",
                    fixture_id,
                    imported["id"],
                    len(result.get("chunks") or []),
                    result.get("embedded"),
                    result.get("cached"),
                )
            except Exception as exc:  # noqa: BLE001 - record the affected cases instead of aborting the suite
                logger.warning(
                    "benchmark fixture indexing failed fixture_id=%s path=%s error=%s",
                    fixture_id,
                    path,
                    exc,
                )
    return indexed


def missing_case_document_ids(case: dict[str, Any], document_ids: dict[str, str]) -> list[str]:
    required_ids = {
        str(document.get("id") or "")
        for document in case.get("documents", [])
        if isinstance(document, dict)
    }
    required_ids.add(str(case.get("required_source_document") or ""))
    return sorted(fixture_id for fixture_id in required_ids if fixture_id and fixture_id not in document_ids)


def normalize_benchmark_mode(value: str | None) -> str:
    normalized = (value or DEFAULT_BENCHMARK_MODE).strip().lower().replace("-", "_").replace(" ", "_")
    if normalized in {"retrieval", "retrieval_only"}:
        normalized = "retrieval_only"
    elif normalized in {"oracle", "oracle_generation"}:
        normalized = "oracle_generation"
    elif normalized in {"rag", "end_to_end", "e2e"}:
        normalized = "end_to_end"
    if normalized not in BENCHMARK_MODES:
        raise ValueError("benchmark_mode must be one of: retrieval_only, oracle_generation, end_to_end")
    return normalized


def benchmark_modes_for_case(case: dict[str, Any]) -> list[str]:
    modes = case.get("benchmark_modes")
    if isinstance(modes, list):
        selected = [normalize_benchmark_mode(str(item)) for item in modes]
        return selected or [DEFAULT_BENCHMARK_MODE]
    return ["retrieval_only", "oracle_generation", "end_to_end"]


def timeout_policy() -> dict[str, int]:
    return {
        "interactive_chat_seconds": 120,
        "benchmark_answer_seconds": BENCHMARK_ANSWER_TIMEOUT_SECONDS,
        "verifier_seconds": VERIFIER_TIMEOUT_SECONDS,
        "correction_seconds": CORRECTION_TIMEOUT_SECONDS,
    }


def options_for_generation(temperature: float, num_predict: int) -> dict[str, Any]:
    options: dict[str, Any] = {"temperature": float(temperature)}
    if int(num_predict or 0) > 0:
        options["num_predict"] = int(num_predict)
    return options


def call_model_detailed(
    models: ModelService,
    model: str,
    messages: list[dict[str, str]],
    options: dict[str, Any],
    *,
    thinking: str,
    timeout: float,
) -> dict[str, Any]:
    started = time.perf_counter()
    detailed_impl = getattr(type(models), "chat_detailed", None)
    if type(models) is ModelService or detailed_impl is not ModelService.chat_detailed:
        try:
            return models.chat_detailed(
                model,
                messages,
                options=options,
                thinking=thinking,
                timeout=timeout,
            )
        except AttributeError:
            pass
        except TypeError:
            pass
    try:
        content = models.chat(model, messages, options=options)
    except TypeError:
        content = models.chat(model, messages)
    return {
        "model": model,
        "content": content,
        "thinking": "",
        "done_reason": "",
        "total_duration_ns": 0,
        "load_duration_ns": 0,
        "prompt_eval_count": 0,
        "prompt_eval_duration_ns": 0,
        "eval_count": 0,
        "eval_duration_ns": 0,
        "prompt_tokens_per_second": None,
        "generation_tokens_per_second": None,
        "elapsed_ms": int((time.perf_counter() - started) * 1000),
        "raw": {},
    }


def oracle_system_prompt(answer_style: str) -> str:
    return (
        "You are answering a benchmark question from supplied gold evidence. "
        "Use only the evidence below. Preserve chronology, quantities, and negation. "
        f"Answer style: {answer_style}."
    )


def oracle_prompt(case: dict[str, Any], gold_evidence: str) -> str:
    return (
        f"Question:\n{case.get('question')}\n\n"
        f"Gold evidence:\n{gold_evidence}\n\n"
        "Answer using only the gold evidence. If the evidence does not contain the requested fact, "
        "say it cannot be confirmed."
    )


def gold_evidence_for_case(case: dict[str, Any]) -> str:
    explicit = case.get("gold_evidence") or case.get("gold_chunk_text")
    if explicit:
        return str(explicit)
    documents = case.get("documents") if isinstance(case.get("documents"), list) else []
    required = str(case.get("required_source_document") or "")
    case_path = Path(str(case.get("_path") or "."))
    for document in documents:
        if str(document.get("id") or "") != required:
            continue
        path = (case_path.parent / str(document.get("path") or "")).resolve()
        if path.exists():
            return path.read_text(encoding="utf-8")
    return ""


def benchmark_prompt_snapshot(question: str, snippets: list[dict[str, Any]], answer_style: str) -> str:
    return (
        f"Question:\n{question}\n\n"
        f"Answer style: {answer_style}\n\n"
        f"Supplied evidence:\n{format_snippets_for_verifier(snippets)}"
    )


def search_result_for_ids(result: Any) -> dict[str, Any]:
    return {
        "chunk_id": getattr(result, "chunk_id", ""),
        "document_id": getattr(result, "document_id", ""),
        "score": float(getattr(result, "score", 0.0)),
    }


def base_case_result(
    case: dict[str, Any],
    *,
    mode: str,
    thinking_mode: str,
    repeat_index: int,
    answer_style: str,
    temperature: float,
    status: str,
    passed: bool,
    expected_passed: bool,
    forbidden_passed: bool,
    source_passed: bool,
    latency_ms: int,
    reasons: list[str],
    retrieved_chunks: list[dict[str, Any]],
    embedding_backend: str,
    embedding_model: str,
    retrieval_metrics: dict[str, Any] | None = None,
    retrieval_candidates: list[dict[str, Any]] | None = None,
    supplied_evidence: list[dict[str, Any]] | None = None,
    prompt_text: str = "",
    answer_content: str = "",
    thinking_text: str = "",
    model_response: dict[str, Any] | None = None,
    grader_matches: list[dict[str, Any]] | None = None,
    grader_review_required: bool = False,
    timings: dict[str, Any] | None = None,
    pipeline_diagnosis: str = "",
    stage: str = "",
    error_message: str = "",
) -> dict[str, Any]:
    return {
        "case_id": str(case["id"]),
        "question": str(case["question"]),
        "answer_style": answer_style,
        "required_source_document": str(case["required_source_document"]),
        "passed": bool(passed),
        "expected_passed": bool(expected_passed),
        "forbidden_passed": bool(forbidden_passed),
        "source_passed": bool(source_passed),
        "latency_ms": int(latency_ms),
        "reasons": reasons,
        "retrieved_document_ids": retrieved_document_ids(retrieved_chunks),
        "retrieved_chunk_ids": retrieved_chunk_ids(retrieved_chunks),
        "embedding_backend": embedding_backend,
        "embedding_model": embedding_model,
        "temperature": float(temperature),
        "case_category": str(case.get("category") or "legacy"),
        "case_difficulty": str(case.get("difficulty") or ""),
        "benchmark_mode": mode,
        "thinking_mode": thinking_mode,
        "repeat_index": int(repeat_index),
        "status": status,
        "stage": stage,
        "pipeline_diagnosis": pipeline_diagnosis,
        "counts_toward_primary": bool(case.get("counts_toward_primary_recommendation", True)),
        "grader_review_required": grader_review_required,
        "answer_content": answer_content,
        "thinking_text": thinking_text,
        "prompt_text": prompt_text,
        "corrected_answer": "",
        "model_response": model_response or {},
        "retrieval_metrics": retrieval_metrics or {},
        "retrieval_candidates": retrieval_candidates or [],
        "supplied_evidence": supplied_evidence or [],
        "grader_matches": grader_matches or [],
        "timings": timings or {},
        "error_message": error_message,
    }


def timeout_case_result(
    case: dict[str, Any],
    *,
    mode: str,
    thinking_mode: str,
    repeat_index: int,
    temperature: float,
    stage: str,
    error: str,
) -> dict[str, Any]:
    return base_case_result(
        case,
        mode=mode,
        thinking_mode=thinking_mode,
        repeat_index=repeat_index,
        answer_style=str(case.get("answer_style") or "precise"),
        temperature=temperature,
        status="timeout",
        passed=False,
        expected_passed=False,
        forbidden_passed=True,
        source_passed=False,
        latency_ms=0,
        reasons=[f"timeout during {stage}"],
        retrieved_chunks=[],
        embedding_backend="",
        embedding_model="",
        pipeline_diagnosis="timeout",
        stage=stage,
        error_message=error,
    )


def error_case_result(
    case: dict[str, Any],
    *,
    mode: str,
    thinking_mode: str,
    repeat_index: int,
    temperature: float,
    stage: str,
    error: str,
) -> dict[str, Any]:
    return base_case_result(
        case,
        mode=mode,
        thinking_mode=thinking_mode,
        repeat_index=repeat_index,
        answer_style=str(case.get("answer_style") or "precise"),
        temperature=temperature,
        status="error",
        passed=False,
        expected_passed=False,
        forbidden_passed=True,
        source_passed=False,
        latency_ms=0,
        reasons=[error],
        retrieved_chunks=[],
        embedding_backend="",
        embedding_model="",
        pipeline_diagnosis="runtime_error",
        stage=stage,
        error_message=error,
    )


def retrieval_metrics_for_case(
    case: dict[str, Any],
    audit: dict[str, Any],
    required_document_id: str,
) -> dict[str, Any]:
    candidates = list(audit.get("candidates") or [])
    ranked_doc_ids = [str(item.get("document_id") or "") for item in candidates]
    ranked_chunk_ids = [str(item.get("chunk_id") or "") for item in candidates]
    vector_rank = next(
        (int(item.get("original_vector_rank") or 0) for item in candidates if item.get("document_id") == required_document_id),
        0,
    )
    rerank = next(
        (int(item.get("final_reranked_rank") or 0) for item in candidates if item.get("document_id") == required_document_id),
        0,
    )
    reciprocal_rank = 1.0 / rerank if rerank else 0.0
    top_k = candidates[:5]
    contaminating = sum(1 for item in top_k if item.get("document_id") != required_document_id)
    first = next((item for item in candidates if item.get("document_id") == required_document_id), {})
    return {
        "required_document_rank_before_reranking": vector_rank,
        "required_document_rank_after_reranking": rerank,
        "required_chunk_rank": required_chunk_rank(case, candidates),
        "hit_at1": rerank == 1,
        "hit_at3": bool(rerank and rerank <= 3),
        "hit_at5": bool(rerank and rerank <= 5),
        "reciprocal_rank": reciprocal_rank,
        "candidate_contaminating_document_count_top_k": contaminating,
        "ranked_retrieved_document_ids": ranked_doc_ids,
        "ranked_retrieved_chunk_ids": ranked_chunk_ids,
        "vector_similarity_score": float(first.get("original_vector_score") or 0.0),
        "lexical_rerank_contribution": float(first.get("lexical_overlap_contribution") or 0.0),
        "metadata_rerank_contribution": float(first.get("metadata_contribution") or 0.0),
        "phrase_bonus": float(first.get("phrase_bonus") or 0.0),
        "final_combined_score": float(first.get("final_combined_score") or 0.0),
        "embedding_backend": str(audit.get("embedding_backend") or ""),
        "embedding_model": str(audit.get("embedding_model") or ""),
        "retrieval_latency_ms": int(audit.get("retrieval_latency_ms") or 0),
    }


def required_chunk_rank(case: dict[str, Any], candidates: list[dict[str, Any]]) -> int:
    marker = str(case.get("gold_chunk_marker") or case.get("gold_chunk_text") or "").strip()
    if not marker:
        return 0
    normalized_marker = normalize(marker)
    for item in candidates:
        content = str(item.get("content") or "")
        if normalized_marker and normalized_marker in normalize(content):
            return int(item.get("final_reranked_rank") or 0)
    return 0


def evaluate_case(case: dict[str, Any], result: dict[str, Any], required_document_id: str) -> dict[str, Any]:
    answer = str(result["assistant_message"]["content"])
    retrieved_snippets = result.get("retrieved_snippets") or result.get("retrieved_chunks") or []
    return evaluate_answer(
        case,
        answer,
        supplied_document_ids=retrieved_document_ids(retrieved_snippets),
        required_document_id=required_document_id,
    )


def evaluate_answer(
    case: dict[str, Any],
    answer: str,
    *,
    supplied_document_ids: list[str],
    required_document_id: str | None,
) -> dict[str, Any]:
    reasons: list[str] = []
    matches: list[dict[str, Any]] = []
    grader_review_required = False

    expected_passed = True
    expected_absence_matched = False
    for expected in case.get("expected_facts", []):
        result = best_match_for_group(answer, expected, mode="expected", expected_abstention=bool(case.get("expected_abstention")))
        matches.append(result)
        expected_absence_matched = expected_absence_matched or (
            result.get("fact_type") in ABSENCE_TYPES and bool(result.get("matched"))
        )
        if result["final_decision"] == "review":
            grader_review_required = True
            expected_passed = False
            reasons.append(f"grader review required for expected fact: {expected.get('label')}")
        elif not result["matched"]:
            expected_passed = False
            reasons.append(f"missing expected fact: {expected.get('label')}")

    forbidden_passed = True
    for forbidden in case.get("forbidden_claims", []):
        result = best_match_for_group(answer, forbidden, mode="forbidden")
        matches.append(result)
        if result["final_decision"] == "review":
            grader_review_required = True
            forbidden_passed = False
            reasons.append(f"grader review required for forbidden claim: {forbidden.get('label')}")
        elif result["matched"]:
            forbidden_passed = False
            reasons.append(f"forbidden claim present: {forbidden.get('label')}")

    if bool(case.get("expected_abstention")):
        abstained = expected_absence_matched or answer_abstains(answer)
        matches.append(
            {
                "kind": "abstention",
                "label": "expected abstention",
                "matched_phrase": "",
                "answer_window": answer[:240],
                "token_overlap_score": 1.0 if abstained else 0.0,
                "nearby_negation_detected": False,
                "matched": abstained,
                "final_decision": "match" if abstained else "no_match",
                "decision_reason": "answer clearly abstained" if abstained else "answer did not clearly abstain",
                "answer_segment": "answer_conclusion",
            }
        )
        if not abstained:
            expected_passed = False
            reasons.append("answer did not clearly abstain")

    source_result = evaluate_source_policy(case, supplied_document_ids, required_document_id)
    source_passed = bool(source_result["passed"])
    matches.append(source_result)
    if not source_passed:
        reasons.append(str(source_result["decision_reason"]))

    passed = expected_passed and forbidden_passed and source_passed and not grader_review_required
    return {
        "passed": passed,
        "expected_passed": expected_passed,
        "forbidden_passed": forbidden_passed,
        "source_passed": source_passed,
        "grader_review_required": grader_review_required,
        "reasons": reasons,
        "matches": matches,
    }


def evaluate_source_policy(
    case: dict[str, Any],
    supplied_document_ids: list[str],
    required_document_id: str | None,
) -> dict[str, Any]:
    policy = normalize_source_policy(str(case.get("source_policy") or "required_present"))
    clean_supplied = [item for item in supplied_document_ids if item]
    nonrequired = [item for item in clean_supplied if required_document_id and item != required_document_id]
    required_present = bool(required_document_id and required_document_id in clean_supplied)
    if not required_document_id:
        passed = True
        reason = "no required source configured"
    elif policy in {"required_present", "abstention_source"}:
        passed = required_present
        reason = "required source was supplied" if passed else "required source was not supplied"
    elif policy == "exclusive_source":
        passed = required_present and not nonrequired
        reason = "only required source was supplied" if passed else "supplied evidence included a non-required source"
    elif policy == "no_conflicting_evidence":
        passed = required_present and not nonrequired
        reason = "no conflicting supplied evidence" if passed else "conflicting or missing supplied evidence"
    else:
        passed = required_present
        reason = "required source was supplied" if passed else "required source was not supplied"
    return {
        "kind": "source_policy",
        "label": policy,
        "matched_phrase": "",
        "answer_window": "",
        "token_overlap_score": 1.0 if passed else 0.0,
        "nearby_negation_detected": False,
        "matched": passed,
        "passed": passed,
        "final_decision": "match" if passed else "no_match",
        "decision_reason": reason,
        "source_policy": policy,
        "required_document_id": required_document_id or "",
        "supplied_document_ids": clean_supplied,
        "conflicting_supplied_document_ids": nonrequired,
    }


def normalize_source_policy(value: str) -> str:
    normalized = value.strip().lower().replace("-", "_").replace(" ", "_")
    if normalized in {"", "all_retrieved", "top_retrieved", "required", "required_source"}:
        return "required_present"
    if normalized in {"exclusive", "exclusive_source", "only_required"}:
        return "exclusive_source"
    if normalized in {"no_conflicting", "no_conflicting_evidence"}:
        return "no_conflicting_evidence"
    if normalized in {"abstention", "abstention_source"}:
        return "abstention_source"
    return "required_present"


def best_match_for_group(
    answer: str,
    group: dict[str, Any],
    *,
    mode: str,
    expected_abstention: bool = False,
) -> dict[str, Any]:
    phrases = [str(phrase) for phrase in group.get("any", []) if str(phrase).strip()]
    fact_type = fact_type_for_group(group, phrases, mode=mode, expected_abstention=expected_abstention)
    label = str(group.get("label") or "")
    if fact_type in RELATION_TYPES:
        return match_relation(answer, group, mode=mode, label=label, fact_type=fact_type)
    if fact_type in ABSENCE_TYPES:
        return match_absence(answer, group, mode=mode, label=label, fact_type=fact_type)
    if not phrases:
        return match_record(mode, label, "", "", 0.0, False, False, "no_match", "no phrases configured", fact_type)
    results = [
        match_phrase(answer, phrase, mode=mode, label=label, fact_type=fact_type)
        for phrase in phrases
    ]
    if mode == "forbidden":
        failing = [item for item in results if item["matched"]]
        if failing:
            return sorted(failing, key=lambda item: -float(item["token_overlap_score"]))[0]
        review = [item for item in results if item["final_decision"] == "review"]
        if review:
            return review[0]
        return sorted(results, key=lambda item: -float(item["token_overlap_score"]))[0]
    matched = [item for item in results if item["matched"]]
    if matched:
        return sorted(matched, key=lambda item: -float(item["token_overlap_score"]))[0]
    review = [item for item in results if item["final_decision"] == "review"]
    if review:
        return review[0]
    return sorted(results, key=lambda item: -float(item["token_overlap_score"]))[0]


def fact_type_for_group(
    group: dict[str, Any],
    phrases: list[str],
    *,
    mode: str,
    expected_abstention: bool,
) -> str:
    explicit = str(group.get("type") or group.get("polarity") or "").strip().lower().replace("-", "_").replace(" ", "_")
    aliases = {
        "positive": "positive_fact",
        "negative": "negative_fact",
        "abstention": "absence_or_abstention",
        "absence": "absence_or_abstention",
        "date": "date_or_time",
        "time": "date_or_time",
    }
    explicit = aliases.get(explicit, explicit)
    if explicit in POSITIVE_TYPES | NEGATIVE_TYPES | ABSENCE_TYPES | EXACT_TYPES | RELATION_TYPES:
        return explicit
    if any(group.get(key) for key in ("subject_aliases", "predicate_aliases", "object_aliases", "value_aliases")):
        return "relation"
    if mode == "expected" and expected_abstention:
        return "absence_or_abstention"
    joined = " ".join(phrases)
    normalized = normalize(joined)
    if CODE_RE.search(joined):
        return "code"
    if TIME_RE.search(joined):
        return "date"
    if exact_identifier_like(normalized):
        return "exact_identifier"
    if NUMBER_RE.search(joined) or any(word in normalized.split() for word in NUMBER_WORDS):
        return "quantity"
    if starts_with_negation(normalized) or " cannot " in f" {normalized} " or " does not " in f" {normalized} ":
        return "negative_fact"
    return "positive_fact"


def exact_identifier_like(text: str) -> bool:
    if re.search(r"\b(slot|dock|box|code|id|identifier)\s+[a-z0-9]+\b", text):
        return True
    tokens = text.split()
    return len(tokens) <= 3 and any(len(token) <= 2 and any(ch.isdigit() for ch in token) for token in tokens)


def starts_with_negation(text: str) -> bool:
    tokens = text.split()
    return bool(tokens and tokens[0] in {"no", "not", "never", "without", "cannot"})


def match_phrase(answer: str, phrase: str, *, mode: str, label: str, fact_type: str) -> dict[str, Any]:
    normalized_answer = normalize(answer)
    normalized_phrase = normalize(phrase)
    phrase_tokens = fact_tokens(normalized_phrase)
    answer_tokens = fact_tokens(normalized_answer)
    overlap = len(answer_tokens & phrase_tokens) / len(phrase_tokens) if phrase_tokens else 0.0
    window, span_start, span_end = answer_clause_for_phrase(answer, phrase, fact_type=fact_type)
    negated = scoped_negation(window, phrase)
    segment_kind = segment_kind_for_span(answer, span_start, span_end, window)
    phrase_exact = bool(normalized_phrase and normalized_phrase in normalized_answer)
    exact = exact_phrase_match(normalized_answer, normalized_phrase, answer, phrase, fact_type=fact_type, window=window)

    if mode == "forbidden":
        strong = exact if fact_type in EXACT_TYPES else (
            exact or (phrase_tokens and (phrase_tokens.issubset(answer_tokens) if len(phrase_tokens) <= 2 else overlap >= 0.86))
        )
        if strong and fact_type in EXACT_TYPES and not phrase_exact:
            anchor = predicate_anchor_token(phrase, fact_type=fact_type)
            if anchor and anchor not in fact_tokens(window):
                return match_record(mode, label, phrase, window, overlap, negated, False, "no_match", "forbidden exact value matched without predicate", fact_type, span_start, span_end, segment_kind=segment_kind)
        if not strong:
            return match_record(mode, label, phrase, window, overlap, negated, False, "no_match", "forbidden phrase not present", fact_type, span_start, span_end, segment_kind=segment_kind)
        if negated == "clear":
            return match_record(mode, label, phrase, window, overlap, True, False, "no_match", "scoped negation rejects forbidden match", fact_type, span_start, span_end, segment_kind=segment_kind)
        if negated == "ambiguous":
            return match_record(mode, label, phrase, window, overlap, True, False, "review", "negation scope is ambiguous", fact_type, span_start, span_end, segment_kind=segment_kind)
        if segment_kind == "evidence_quotation" and not evidence_segment_adopted(answer, span_start, span_end):
            return match_record(mode, label, phrase, window, overlap, False, False, "no_match", "quoted evidence was not adopted as the answer conclusion", fact_type, span_start, span_end, segment_kind=segment_kind)
        return match_record(mode, label, phrase, window, overlap, False, True, "match", "affirmative forbidden claim matched", fact_type, span_start, span_end, segment_kind=segment_kind)

    if fact_type in ABSENCE_TYPES:
        return match_absence(answer, {"label": label, "any": [phrase]}, mode=mode, label=label, fact_type=fact_type)

    strong = exact if fact_type in EXACT_TYPES else (
        exact or (phrase_tokens and (phrase_tokens.issubset(answer_tokens) if len(phrase_tokens) <= 2 else overlap >= 0.62))
    )
    if not strong:
        return match_record(mode, label, phrase, window, overlap, bool(negated), False, "no_match", "expected phrase not present", fact_type, span_start, span_end, segment_kind=segment_kind)
    if fact_type not in NEGATIVE_TYPES:
        if negated == "ambiguous":
            return match_record(mode, label, phrase, window, overlap, True, False, "review", "expected fact appears in ambiguous negation", fact_type, span_start, span_end, segment_kind=segment_kind)
        if negated == "clear":
            return match_record(mode, label, phrase, window, overlap, True, False, "no_match", "expected fact appears negated", fact_type, span_start, span_end, segment_kind=segment_kind)
    return match_record(mode, label, phrase, window, overlap, bool(negated), True, "match", "expected fact matched", fact_type, span_start, span_end, segment_kind=segment_kind)


def match_record(
    kind: str,
    label: str,
    phrase: str,
    window: str,
    overlap: float,
    negated: bool,
    matched: bool,
    decision: str,
    reason: str,
    fact_type: str = "",
    span_start: int = -1,
    span_end: int = -1,
    *,
    segment_kind: str = "answer_conclusion",
    details: dict[str, Any] | None = None,
) -> dict[str, Any]:
    record = {
        "kind": kind,
        "label": label,
        "fact_type": fact_type,
        "matched_phrase": phrase,
        "answer_window": window,
        "answer_span": {"start": span_start, "end": span_end},
        "token_overlap_score": overlap,
        "nearby_negation_detected": negated,
        "matched": matched,
        "final_decision": decision,
        "decision_reason": reason,
        "answer_segment": segment_kind,
    }
    if details:
        record.update(details)
    return record


def match_absence(
    answer: str,
    group: dict[str, Any],
    *,
    mode: str,
    label: str,
    fact_type: str,
) -> dict[str, Any]:
    targets = assertion_aliases(group, "target_aliases") or absence_targets_from_group(group)
    phrases = [str(item) for item in group.get("any", []) if str(item).strip()]
    best_clause = ""
    best_span = (-1, -1)
    best_pattern = ""
    best_score = 0.0
    best_segment = "answer_conclusion"
    for clause, start, end in split_answer_clauses(answer):
        pattern = matched_absence_construction(clause)
        target = matched_target_alias(clause, targets)
        score = 0.0
        if pattern:
            score += 0.55
        if target:
            score += 0.45
        if not target and any(normalize(phrase) and normalize(phrase) in normalize(clause) for phrase in phrases):
            target = label
            score += 0.35
        if score > best_score:
            best_clause = " ".join(clause.split())
            best_span = (start, end)
            best_pattern = pattern
            best_score = score
            best_segment = segment_kind_for_span(answer, start, end, clause)

    mixed = mixed_absence_with_invention(answer, targets)
    details = {
        "absence_construction": best_pattern,
        "target_concept": ", ".join(targets[:4]),
        "containing_clause": best_clause,
        "absence_decision": "mixed_invention" if mixed else ("match" if best_pattern and best_score >= 0.75 else "no_match"),
    }

    if mode == "forbidden":
        matched = bool(best_pattern and best_score >= 0.75 and not mixed)
        return match_record(
            mode,
            label,
            phrases[0] if phrases else "",
            best_clause,
            min(best_score, 1.0),
            False,
            matched,
            "match" if matched else "no_match",
            "forbidden absence assertion matched" if matched else "forbidden absence assertion not present",
            fact_type,
            best_span[0],
            best_span[1],
            segment_kind=best_segment,
            details=details,
        )

    if mixed:
        return match_record(
            mode,
            label,
            phrases[0] if phrases else "",
            best_clause,
            min(best_score, 1.0),
            False,
            False,
            "no_match",
            "absence statement was contradicted by an affirmative invented claim",
            fact_type,
            best_span[0],
            best_span[1],
            segment_kind=best_segment,
            details=details,
        )
    matched = bool(best_pattern and best_score >= 0.75)
    return match_record(
        mode,
        label,
        phrases[0] if phrases else "",
        best_clause,
        min(best_score, 1.0),
        False,
        matched,
        "match" if matched else "no_match",
        "answer clearly states the requested information is absent" if matched else "answer did not clearly state absence for the target concept",
        fact_type,
        best_span[0],
        best_span[1],
        segment_kind=best_segment,
        details=details,
    )


def match_relation(
    answer: str,
    group: dict[str, Any],
    *,
    mode: str,
    label: str,
    fact_type: str,
) -> dict[str, Any]:
    subject_aliases = assertion_aliases(group, "subject_aliases")
    predicate_aliases = assertion_aliases(group, "predicate_aliases")
    object_aliases = assertion_aliases(group, "object_aliases")
    value_aliases = assertion_aliases(group, "value_aliases")
    best: dict[str, Any] | None = None
    any_components = relation_components_present(answer, subject_aliases, predicate_aliases, object_aliases, value_aliases)
    for clause, start, end in relation_candidate_clauses(answer):
        components = {
            "subject": matched_target_alias(clause, subject_aliases),
            "predicate": matched_predicate_alias(clause, predicate_aliases),
            "object": matched_target_alias(clause, object_aliases) if object_aliases else "",
            "value": matched_value_alias(clause, value_aliases),
        }
        required = [components["subject"], components["predicate"], components["value"]]
        if object_aliases:
            required.append(components["object"])
        score = sum(1 for item in required if item) / max(len(required), 1)
        if best is None or score > float(best.get("score") or 0.0):
            best = {
                "clause": " ".join(clause.split()),
                "start": start,
                "end": end,
                "components": components,
                "score": score,
                "negated": scoped_negation(clause, " ".join(subject_aliases + value_aliases)) == "clear",
                "segment": segment_kind_for_span(answer, start, end, clause),
            }
        if score >= 1.0:
            break

    best = best or {"clause": "", "start": -1, "end": -1, "components": {}, "score": 0.0, "negated": False, "segment": "answer_conclusion"}
    matched = float(best.get("score") or 0.0) >= 1.0 and not bool(best.get("negated"))
    details = {
        "relation_components": best.get("components") or {},
        "relation_clause": best.get("clause") or "",
        "relation_scope": str(group.get("scope") or "same_clause"),
    }
    if mode == "forbidden":
        if matched and best.get("segment") == "evidence_quotation" and not evidence_segment_adopted(answer, span_value(best, "start"), span_value(best, "end")):
            return match_record(
                mode,
                label,
                relation_label(group),
                str(best.get("clause") or ""),
                float(best.get("score") or 0.0),
                False,
                False,
                "no_match",
                "quoted evidence relation was not adopted as the answer conclusion",
                fact_type,
                span_value(best, "start"),
                span_value(best, "end"),
                segment_kind=str(best.get("segment") or "answer_conclusion"),
                details=details,
            )
        return match_record(
            mode,
            label,
            relation_label(group),
            str(best.get("clause") or ""),
            float(best.get("score") or 0.0),
            bool(best.get("negated")),
            matched,
            "match" if matched else "no_match",
            "forbidden relation matched within one clause" if matched else "forbidden relation components were not bound in one clause",
            fact_type,
            span_value(best, "start"),
            span_value(best, "end"),
            segment_kind=str(best.get("segment") or "answer_conclusion"),
            details=details,
        )
    if matched:
        return match_record(
            mode,
            label,
            relation_label(group),
            str(best.get("clause") or ""),
            float(best.get("score") or 0.0),
            False,
            True,
            "match",
            "expected relation matched within one clause",
            fact_type,
            span_value(best, "start"),
            span_value(best, "end"),
            segment_kind=str(best.get("segment") or "answer_conclusion"),
            details=details,
        )
    if any_components:
        return match_record(
            mode,
            label,
            relation_label(group),
            str(best.get("clause") or ""),
            float(best.get("score") or 0.0),
            bool(best.get("negated")),
            False,
            "review",
            "relation components appeared but were not safely bound",
            fact_type,
            span_value(best, "start"),
            span_value(best, "end"),
            segment_kind=str(best.get("segment") or "answer_conclusion"),
            details=details,
        )
    return match_record(
        mode,
        label,
        relation_label(group),
        str(best.get("clause") or ""),
        float(best.get("score") or 0.0),
        False,
        False,
        "no_match",
        "expected relation not present",
        fact_type,
        span_value(best, "start"),
        span_value(best, "end"),
        segment_kind=str(best.get("segment") or "answer_conclusion"),
        details=details,
    )


def assertion_aliases(group: dict[str, Any], key: str) -> list[str]:
    value = group.get(key)
    if not isinstance(value, list):
        return []
    return [str(item).strip() for item in value if str(item).strip()]


def absence_targets_from_group(group: dict[str, Any]) -> list[str]:
    targets = []
    label = str(group.get("label") or "").strip()
    if label:
        targets.append(label)
    for phrase in group.get("any") or []:
        text = normalize(str(phrase))
        for verb in ABSENCE_VERBS:
            text = re.sub(rf"\b(?:does|do|did)\s+not\s+{verb}\b", " ", text)
            text = re.sub(rf"\bcannot\s+{verb}\b", " ", text)
        text = re.sub(r"\b(no|not|cannot|can|the|a|an|who|what|whether|if)\b", " ", text)
        cleaned = " ".join(token for token in text.split() if token not in FACT_STOPWORDS)
        if cleaned:
            targets.append(cleaned)
    return dedupe_labels(targets)


def matched_absence_construction(clause: str) -> str:
    for regex in ABSENCE_REGEXES:
        match = regex.search(clause or "")
        if match:
            return " ".join(match.group(0).split())
    return ""


def matched_target_alias(clause: str, aliases: list[str]) -> str:
    normalized_clause = normalize(clause)
    clause_tokens = fact_tokens(normalized_clause)
    for alias in aliases:
        normalized_alias = normalize(alias)
        if not normalized_alias:
            continue
        alias_tokens = fact_tokens(normalized_alias)
        if normalized_alias in normalized_clause:
            return alias
        if alias_tokens and alias_tokens.issubset(clause_tokens):
            return alias
    return ""


def matched_predicate_alias(clause: str, aliases: list[str]) -> str:
    clause_tokens = fact_tokens(clause)
    for alias in aliases:
        alias_tokens = fact_tokens(alias)
        if alias_tokens and alias_tokens.issubset(clause_tokens):
            return alias
    return ""


def matched_value_alias(clause: str, aliases: list[str]) -> str:
    for alias in aliases:
        if exact_values(alias, fact_type="quantity").issubset(exact_values(clause, fact_type="quantity")) and exact_values(alias, fact_type="quantity"):
            return alias
        if exact_values(alias, fact_type="date_or_time").issubset(exact_values(clause, fact_type="date_or_time")) and exact_values(alias, fact_type="date_or_time"):
            return alias
        if matched_target_alias(clause, [alias]):
            return alias
    return ""


def mixed_absence_with_invention(answer: str, targets: list[str]) -> bool:
    saw_absence = False
    for clause, _, _ in split_answer_clauses(answer):
        has_absence = bool(matched_absence_construction(clause))
        target = matched_target_alias(clause, targets)
        if has_absence and target:
            saw_absence = True
            continue
        if saw_absence and clause_starts_with_contrast(clause) and target and scoped_negation(clause, target) != "clear":
            return True
    return False


def clause_starts_with_contrast(clause: str) -> bool:
    tokens = normalize(clause).split()
    return bool(tokens and tokens[0] in CONTRAST_MARKERS)


def relation_candidate_clauses(answer: str) -> list[tuple[str, int, int]]:
    return split_answer_clauses(answer)


def relation_components_present(
    answer: str,
    subject_aliases: list[str],
    predicate_aliases: list[str],
    object_aliases: list[str],
    value_aliases: list[str],
) -> bool:
    return bool(
        matched_target_alias(answer, subject_aliases)
        and matched_predicate_alias(answer, predicate_aliases)
        and matched_value_alias(answer, value_aliases)
        and (not object_aliases or matched_target_alias(answer, object_aliases))
    )


def span_value(item: dict[str, Any], key: str) -> int:
    value = item.get(key)
    return int(value) if value is not None else -1


def relation_label(group: dict[str, Any]) -> str:
    subject = "/".join(assertion_aliases(group, "subject_aliases")[:2])
    value = "/".join(assertion_aliases(group, "value_aliases")[:2])
    return f"{subject} -> {value}".strip(" ->") or str(group.get("label") or "relation")


def segment_kind_for_span(answer: str, start: int, end: int, window: str) -> str:
    if start < 0:
        return "answer_conclusion"
    prefix = answer[max(0, start - 120) : start].lower()
    line_start = answer.rfind("\n", 0, max(start, 0)) + 1
    line = answer[line_start:end].strip().lower()
    normalized_window = normalize(window)
    if (
        "retrieved context says" in prefix
        or "provided evidence" in prefix
        or "evidence snippets" in prefix
        or line.startswith("-")
        or line.startswith("\"")
        or normalized_window.startswith("# ")
    ):
        return "evidence_quotation"
    if "because" in prefix or "therefore" in prefix or "so " in prefix:
        return "explanatory_text"
    return "answer_conclusion"


def evidence_segment_adopted(answer: str, start: int, end: int) -> bool:
    tail = normalize(answer[end : end + 160])
    if any(pattern in tail for pattern in ("therefore", "so the answer", "this means", "the answer is")):
        return True
    prefix = normalize(answer[max(0, start - 80) : start])
    return "answer is" in prefix or "conclusion" in prefix


def exact_phrase_match(
    normalized_answer: str,
    normalized_phrase: str,
    answer: str,
    phrase: str,
    *,
    fact_type: str,
    window: str,
) -> bool:
    if normalized_phrase and normalized_phrase in normalized_answer:
        return True
    if fact_type not in EXACT_TYPES:
        return False
    phrase_values = exact_values(phrase, fact_type=fact_type)
    if not phrase_values:
        return False
    answer_values = exact_values(window or answer, fact_type=fact_type)
    return phrase_values.issubset(answer_values)


def exact_values(text: str, *, fact_type: str) -> set[str]:
    if fact_type == "code":
        return {match.group(0).upper() for match in CODE_RE.finditer(text)}
    if fact_type in {"date", "date_or_time"}:
        normalized = text.upper().replace("A.M.", "AM").replace("P.M.", "PM")
        return {match.group(0) for match in TIME_RE.finditer(normalized)}
    if fact_type == "quantity":
        normalized = normalize_number_words(normalize(text))
        values = {match.group(0) for match in NUMBER_RE.finditer(normalized)}
        units = set()
        tokens = normalized.split()
        for index, token in enumerate(tokens[:-1]):
            if NUMBER_RE.fullmatch(token):
                units.add(f"{token} {tokens[index + 1]}")
        return values | units
    normalized = normalize(text)
    values = set()
    for pattern in (r"\bslot\s+[a-z0-9]+\b", r"\bdock\s+[a-z0-9]+\b", r"\bbox\s+[a-z0-9]+\b"):
        values.update(match.group(0) for match in re.finditer(pattern, normalized))
    values.update(match.group(0).upper() for match in CODE_RE.finditer(text))
    return values or {normalized} if normalized else set()


def predicate_anchor_token(phrase: str, *, fact_type: str) -> str:
    if fact_type in {"code", "date", "date_or_time"}:
        return ""
    value_tokens = set()
    for value in exact_values(phrase, fact_type=fact_type):
        value_tokens.update(fact_tokens(value))
    for raw in WORD_RE.findall(phrase or ""):
        token = stem(raw.lower())
        if token and token not in FACT_STOPWORDS and token not in value_tokens and not NUMBER_RE.fullmatch(token):
            return token
    return ""


def normalize_number_words(text: str) -> str:
    return " ".join(NUMBER_WORDS.get(token, token) for token in text.split())


def answer_clause_for_phrase(answer: str, phrase: str, *, fact_type: str) -> tuple[str, int, int]:
    clauses = split_answer_clauses(answer)
    phrase_values = exact_values(phrase, fact_type=fact_type)
    normalized_phrase = normalize(phrase)
    tokens = fact_tokens(phrase)
    best = (answer or "", 0, len(answer or ""), -1.0)
    for clause, start, end in clauses:
        normalized_clause = normalize(clause)
        if normalized_phrase and normalized_phrase in normalized_clause:
            return " ".join(clause.split()), start, end
        if phrase_values and phrase_values.issubset(exact_values(clause, fact_type=fact_type)):
            return " ".join(clause.split()), start, end
        clause_tokens = fact_tokens(clause)
        overlap = len(tokens & clause_tokens) / len(tokens) if tokens else 0.0
        if overlap > best[3]:
            best = (clause, start, end, overlap)
    return " ".join(best[0].split())[:240], best[1], best[2]


def split_answer_clauses(answer: str) -> list[tuple[str, int, int]]:
    text = answer or ""
    spans: list[tuple[str, int, int]] = []
    start = 0
    for match in re.finditer(r"[.;!?]|\bbut\b|\balthough\b|\bthough\b|\bhowever\b|\byet\b", text, flags=re.IGNORECASE):
        end = match.start()
        part = text[start:end].strip()
        if part:
            spans.append((part, start, end))
        separator = match.group(0)
        start = match.start() if separator.isalpha() else match.end()
    tail = text[start:].strip()
    if tail:
        spans.append((tail, start, len(text)))
    return spans or [(text, 0, len(text))]


def answer_window_for_phrase(answer: str, phrase: str, *, radius: int = 90) -> str:
    normalized_answer = normalize(answer)
    normalized_phrase = normalize(phrase)
    if normalized_phrase and normalized_phrase in normalized_answer:
        index = normalized_answer.find(normalized_phrase)
        return normalized_answer[max(0, index - radius) : index + len(normalized_phrase) + radius]
    tokens = fact_tokens(phrase)
    sentences = split_answer_sentences(answer)
    best = ""
    best_overlap = -1.0
    for sentence in sentences:
        sentence_tokens = fact_tokens(sentence)
        overlap = len(tokens & sentence_tokens) / len(tokens) if tokens else 0.0
        if overlap > best_overlap:
            best = sentence
            best_overlap = overlap
    return " ".join(best.split())[: radius * 2]


def split_answer_sentences(answer: str) -> list[str]:
    parts = re.split(r"(?<=[.!?])\s+", answer or "")
    return [part.strip() for part in parts if part.strip()] or [answer or ""]


def scoped_negation(window: str, phrase: str) -> str | None:
    text = normalize(window)
    phrase_text = normalize(phrase)
    if "not only" in text:
        return "ambiguous"
    anchor = re.escape(first_content_token(phrase_text))
    if not anchor:
        return None
    clear_patterns = [
        r"\bno\b[^,;.!?]{0,45}\b" + anchor,
        r"\bnot\b[^,;.!?]{0,55}\b" + anchor,
        r"\bdoes not\b[^,;.!?]{0,70}\b(confirm|establish|show|say|state)?[^,;.!?]{0,35}\b" + anchor,
        r"\bdid not\b[^,;.!?]{0,55}\b" + anchor,
        r"\bcannot confirm\b",
        r"\bcan not confirm\b",
        r"\bnot confirm\b[^,;.!?]{0,45}\b" + anchor,
        r"\bnot establish\b[^,;.!?]{0,45}\b" + anchor,
        r"\bincorrect to say\b[^,;.!?]{0,55}\b" + anchor,
    ]
    if any(re.search(pattern, text) for pattern in clear_patterns):
        return "clear"
    if any(token in NEGATION_WORDS for token in text.split()):
        return "ambiguous"
    return None


def nearby_negation(window: str, phrase: str) -> str | None:
    return scoped_negation(window, phrase)


def first_content_token(text: str) -> str:
    generic = {"there", "document", "confirms", "confirm", "establish", "establishes", "says", "say"}
    tokens = [token for token in WORD_RE.findall(text) if token not in FACT_STOPWORDS and token not in generic]
    return tokens[0] if tokens else ""


def answer_abstains(answer: str) -> bool:
    return any(matched_absence_construction(clause) for clause, _, _ in split_answer_clauses(answer))


def diagnose_generation_only(grading: dict[str, Any]) -> str:
    return canonical_terminal_diagnosis(
        grader_review_required=bool(grading.get("grader_review_required")),
        passed=bool(grading.get("passed")),
        retrieval_failed=False,
        generation_failed=not bool(grading.get("passed")),
    )


def diagnose_end_to_end(
    retrieval_metrics: dict[str, Any],
    grading: dict[str, Any],
    verifier: dict[str, Any],
) -> str:
    _ = retrieval_metrics
    retrieval_failed = not bool(grading.get("source_passed"))
    generation_failed = not (grading.get("expected_passed") and grading.get("forbidden_passed"))
    return canonical_terminal_diagnosis(
        verifier_status=str(verifier.get("status") or ""),
        grader_review_required=bool(grading.get("grader_review_required")),
        passed=bool(grading.get("passed")),
        retrieval_failed=retrieval_failed,
        generation_failed=generation_failed,
    )


def case_summary(case: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": str(case.get("id", "")),
        "category": str(case.get("category") or "legacy"),
        "difficulty": str(case.get("difficulty") or ""),
        "benchmark_modes": benchmark_modes_for_case(case),
        "counts_toward_primary_recommendation": bool(
            case.get("counts_toward_primary_recommendation", True)
        ),
        "question": str(case.get("question", "")),
        "answer_style": str(case.get("answer_style") or "precise"),
        "required_source_document": str(case.get("required_source_document", "")),
        "expected_fact_count": len(case.get("expected_facts", [])),
        "forbidden_claim_count": len(case.get("forbidden_claims", [])),
    }


def benchmark_run_dict(row: Any) -> dict[str, Any]:
    return {
        "id": row_get(row, "id", ""),
        "model": row_get(row, "model", ""),
        "verify": bool(row_get(row, "verify", 0)),
        "suite_name": row_get(row, "suite_name", ""),
        "suite_version": row_get(row, "suite_version", ""),
        "total_passed": int(row_get(row, "total_passed", 0)),
        "total_failed": int(row_get(row, "total_failed", 0)),
        "average_latency_ms": int(row_get(row, "average_latency_ms", 0)),
        "total_runtime_ms": int(row_get(row, "total_runtime_ms", 0)),
        "embedding_backend": row_get(row, "embedding_backend", ""),
        "embedding_model": row_get(row, "embedding_model", ""),
        "temperature": float(row_get(row, "temperature", 0)),
        "notes": row_get(row, "notes", ""),
        "created_at": int(row_get(row, "created_at", 0)),
        "app_version": row_get(row, "app_version", ""),
        "prompt_version": row_get(row, "prompt_version", ""),
        "benchmark_mode": row_get(row, "benchmark_mode", "end_to_end"),
        "thinking_mode": row_get(row, "thinking_mode", "legacy/unrecorded"),
        "answer_style": row_get(row, "answer_style", ""),
        "status": row_get(row, "status", "completed"),
        "repeat_count": int(row_get(row, "repeat_count", 1)),
        "num_predict": int(row_get(row, "num_predict", 0)),
        "timeout_policy": json_loads(row_get(row, "timeout_policy_json", "{}"), {}),
        "model_info": json_loads(row_get(row, "model_info_json", "{}"), {}),
        "retrieval_score": json_loads(row_get(row, "retrieval_score_json", "{}"), {}),
        "oracle_score": json_loads(row_get(row, "oracle_score_json", "{}"), {}),
        "end_to_end_score": json_loads(row_get(row, "end_to_end_score_json", "{}"), {}),
        "practical_score": json_loads(row_get(row, "practical_score_json", "{}"), {}),
        "adversarial_score": json_loads(row_get(row, "adversarial_score_json", "{}"), {}),
        "timeout_count": int(row_get(row, "timeout_count", 0)),
        "runtime_error_count": int(row_get(row, "runtime_error_count", 0)),
        "grader_review_count": int(row_get(row, "grader_review_count", 0)),
        "completed_at": row_get(row, "completed_at", None),
    }


def benchmark_case_dict(row: Any) -> dict[str, Any]:
    case = {
        "id": row_get(row, "id", ""),
        "run_id": row_get(row, "run_id", ""),
        "case_id": row_get(row, "case_id", ""),
        "question": row_get(row, "question", ""),
        "answer_style": row_get(row, "answer_style", ""),
        "required_source_document": row_get(row, "required_source_document", ""),
        "passed": bool(row_get(row, "passed", 0)),
        "expected_passed": bool(row_get(row, "expected_passed", 0)),
        "forbidden_passed": bool(row_get(row, "forbidden_passed", 0)),
        "source_passed": bool(row_get(row, "source_passed", 0)),
        "latency_ms": int(row_get(row, "latency_ms", 0)),
        "reasons": json_loads(row_get(row, "reasons_json", "[]"), []),
        "retrieved_document_ids": json_loads(row_get(row, "retrieved_document_ids_json", "[]"), []),
        "retrieved_chunk_ids": json_loads(row_get(row, "retrieved_chunk_ids_json", "[]"), []),
        "embedding_backend": row_get(row, "embedding_backend", ""),
        "embedding_model": row_get(row, "embedding_model", ""),
        "temperature": float(row_get(row, "temperature", 0)),
        "created_at": int(row_get(row, "created_at", 0)),
        "case_category": row_get(row, "case_category", ""),
        "case_difficulty": row_get(row, "case_difficulty", ""),
        "benchmark_mode": row_get(row, "benchmark_mode", "end_to_end"),
        "thinking_mode": row_get(row, "thinking_mode", "legacy/unrecorded"),
        "repeat_index": int(row_get(row, "repeat_index", 1)),
        "status": row_get(row, "status", "completed"),
        "stage": row_get(row, "stage", ""),
        "pipeline_diagnosis": row_get(row, "pipeline_diagnosis", ""),
        "counts_toward_primary": bool(row_get(row, "counts_toward_primary", 1)),
        "grader_review_required": bool(row_get(row, "grader_review_required", 0)),
        "answer_content": row_get(row, "answer_content", ""),
        "thinking_text": row_get(row, "thinking_text", ""),
        "thinking_returned": bool(row_get(row, "thinking_returned", 0)),
        "thinking_char_count": int(row_get(row, "thinking_char_count", 0)),
        "prompt_text": row_get(row, "prompt_text", ""),
        "corrected_answer": row_get(row, "corrected_answer", ""),
        "model_response": json_loads(row_get(row, "model_response_json", "{}"), {}),
        "retrieval_metrics": json_loads(row_get(row, "retrieval_metrics_json", "{}"), {}),
        "retrieval_candidates": json_loads(row_get(row, "retrieval_candidates_json", "[]"), []),
        "supplied_evidence": json_loads(row_get(row, "supplied_evidence_json", "[]"), []),
        "grader_matches": json_loads(row_get(row, "grader_matches_json", "[]"), []),
        "timings": json_loads(row_get(row, "timings_json", "{}"), {}),
        "error_message": row_get(row, "error_message", ""),
    }
    case["pipeline_diagnosis"] = canonical_case_diagnosis(case)
    return case


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
    incomplete_runs = []
    for run in runs:
        if str(run.get("suite_version") or "") != current_suite_version:
            excluded_runs.append(run)
        elif str(run.get("status") or "completed") != "completed":
            incomplete_runs.append(run)
        else:
            comparable_runs.append(run)
    excluded_suite_versions = sorted(
        {
            str(run.get("suite_version") or "unknown")
            for run in excluded_runs
        }
    )
    grouped_runs: dict[tuple[Any, ...], list[dict[str, Any]]] = {}
    for run in comparable_runs:
        grouped_runs.setdefault(comparison_key(run), []).append(run)

    groups_by_key: dict[tuple[Any, ...], dict[str, Any]] = {}
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
        practical_rates = [run_practical_pass_rate(run) for run in sorted_runs]
        adversarial_rates = [run_adversarial_pass_rate(run) for run in sorted_runs]
        run_latencies = [run_average_latency_ms(run) for run in sorted_runs]
        run_count = len(sorted_runs)
        total_runtime_ms = sum(int(run.get("total_runtime_ms") or 0) for run in sorted_runs)
        latest_total = run_total_count(latest_run)
        best_total = run_total_count(best_run)
        latest_failed = run_failed_count(latest_run)
        latest_grader_review = run_grader_review_count(latest_run)
        latest_coverage = run_coverage(latest_run)
        stability_label = repeatability_label(run_count)
        chronology = chronology_stats_for_runs(sorted_runs)
        timeout_rate = sum(int(run.get("timeout_count") or 0) for run in sorted_runs) / max(
            sum(run_total_count(run) for run in sorted_runs),
            1,
        )

        group = {
            "key": "|".join(str(item) for item in key),
            "model": str(latest_run.get("model") or ""),
            "embedding_backend": str(latest_run.get("embedding_backend") or ""),
            "embedding_model": str(latest_run.get("embedding_model") or ""),
            "benchmark_mode": str(latest_run.get("benchmark_mode") or "end_to_end"),
            "thinking_mode": str(latest_run.get("thinking_mode") or "legacy/unrecorded"),
            "prompt_version": str(latest_run.get("prompt_version") or ""),
            "answer_style": str(latest_run.get("answer_style") or ""),
            "num_predict": int(latest_run.get("num_predict") or 0),
            "status": str(latest_run.get("status") or "completed"),
            "verify": bool(latest_run.get("verify")),
            "temperature": float(latest_run.get("temperature") or 0),
            "suite_version": str(latest_run.get("suite_version") or ""),
            "run_count": run_count,
            "repeatability_label": stability_label,
            "latest_run_passed": run_passed_count(latest_run),
            "latest_run_failed": latest_failed,
            "latest_run_grader_review": latest_grader_review,
            "latest_run_total": latest_total,
            "latest_run_pass_rate": run_pass_rate(latest_run),
            "latest_run_coverage": latest_coverage,
            "latest_run_adjudicated_total": run_adjudicated_total(latest_run),
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
            "mean_coverage": sum(run_coverage(run) for run in sorted_runs) / run_count if run_count else 0.0,
            "mean_practical_pass_rate": sum(practical_rates) / run_count if run_count else 0.0,
            "worst_run_practical_pass_rate": min(practical_rates) if practical_rates else 0.0,
            "mean_adversarial_pass_rate": sum(adversarial_rates) / run_count if run_count else 0.0,
            "chronology_attempted": chronology["attempted"],
            "chronology_passed": chronology["passed"],
            "chronology_failed": chronology["failed"],
            "chronology_pass_rate": chronology["pass_rate"],
            "timeout_rate": timeout_rate,
            "recommendation_eligible": bool(
                str(latest_run.get("benchmark_mode") or "end_to_end") == "end_to_end"
                and timeout_rate < 0.5
                and run_total_count(latest_run) > 0
                and latest_coverage >= 0.9
            ),
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
                "failed": group["latest_run_failed"],
                "grader_review": group["latest_run_grader_review"],
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
            str(group.get("thinking_mode") or "") != "off",
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
            incomplete_count=len(incomplete_runs),
        ),
        "case_difficulty": case_difficulty_summary(comparable_runs),
        "comparison_suite_version": current_suite_version,
        "included_run_count": len(comparable_runs),
        "excluded_run_count": len(excluded_runs),
        "incomplete_run_count": len(incomplete_runs),
        "excluded_suite_versions": excluded_suite_versions,
    }


def run_cases(run: dict[str, Any]) -> list[dict[str, Any]]:
    cases = run.get("cases")
    return cases if isinstance(cases, list) else []


def run_passed_count(run: dict[str, Any]) -> int:
    return int(run.get("total_passed") or 0)


def primary_score_for_run(run: dict[str, Any]) -> dict[str, Any]:
    mode = str(run.get("benchmark_mode") or "end_to_end")
    if mode == "retrieval_only":
        score = run.get("retrieval_score")
    elif mode == "oracle_generation":
        score = run.get("oracle_score")
    else:
        score = run.get("end_to_end_score")
    return score if isinstance(score, dict) else {}


def run_failed_count(run: dict[str, Any]) -> int:
    score = primary_score_for_run(run)
    if "failed" in score:
        return int(score.get("failed") or 0)
    return int(run.get("total_failed") or 0)


def run_grader_review_count(run: dict[str, Any]) -> int:
    score = primary_score_for_run(run)
    if "grader_review_count" in score:
        return int(score.get("grader_review_count") or 0)
    return int(run.get("grader_review_count") or 0)


def run_total_count(run: dict[str, Any]) -> int:
    cases = run_cases(run)
    if cases:
        return len(cases)
    return run_passed_count(run) + int(run.get("total_failed") or 0)


def run_adjudicated_total(run: dict[str, Any]) -> int:
    score = primary_score_for_run(run)
    if "adjudicated_total" in score:
        return int(score.get("adjudicated_total") or 0)
    return run_passed_count(run) + run_failed_count(run)


def run_coverage(run: dict[str, Any]) -> float:
    score = primary_score_for_run(run)
    if "coverage" in score:
        return float(score.get("coverage") or 0.0)
    total = run_total_count(run)
    return run_adjudicated_total(run) / total if total else 0.0


def run_pass_rate(run: dict[str, Any]) -> float:
    score = primary_score_for_run(run)
    if "adjudicated_pass_rate" in score:
        return float(score.get("adjudicated_pass_rate") or 0.0)
    total = run_adjudicated_total(run)
    return float(run_passed_count(run)) / total if total else 0.0


def run_practical_pass_rate(run: dict[str, Any]) -> float:
    score = run.get("practical_score") if isinstance(run.get("practical_score"), dict) else {}
    if score.get("total"):
        return float(score.get("pass_rate") or 0.0)
    cases = [
        case
        for case in run_cases(run)
        if case.get("counts_toward_primary") and case.get("case_category") != "negation_adversarial"
    ]
    if not cases:
        return run_pass_rate(run)
    return pass_rate_for_cases(cases)


def run_adversarial_pass_rate(run: dict[str, Any]) -> float:
    score = run.get("adversarial_score") if isinstance(run.get("adversarial_score"), dict) else {}
    if score.get("total"):
        return float(score.get("pass_rate") or 0.0)
    cases = [case for case in run_cases(run) if case.get("case_category") == "negation_adversarial"]
    if not cases:
        return 0.0
    return pass_rate_for_cases(cases)


def pass_rate_for_cases(cases: list[dict[str, Any]]) -> float:
    if not cases:
        return 0.0
    passed = sum(1 for case in cases if case.get("passed"))
    failed = sum(1 for case in cases if primary_case_bucket(case) in {"retrieval_only", "generation_only", "both"})
    adjudicated = passed + failed
    return passed / adjudicated if adjudicated else 0.0


def repeatability_label(run_count: int) -> str:
    if run_count >= 3:
        return "Benchmarked"
    if run_count == 2:
        return "Preliminary"
    return "Provisional"


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


def comparison_key(run: dict[str, Any]) -> tuple[str, str, str, str, str, str, str, bool, float, str, int]:
    return (
        str(run.get("suite_version") or ""),
        str(run.get("benchmark_mode") or "end_to_end"),
        str(run.get("model") or ""),
        str(run.get("embedding_backend") or ""),
        str(run.get("embedding_model") or ""),
        str(run.get("thinking_mode") or "legacy/unrecorded"),
        str(run.get("prompt_version") or ""),
        bool(run.get("verify")),
        round(float(run.get("temperature") or 0), 4),
        str(run.get("answer_style") or ""),
        int(run.get("num_predict") or 0),
    )


def no_verifier_key(group: dict[str, Any]) -> tuple[str, str, str, str, str, str, str, bool, float, str, int]:
    return (
        str(group.get("suite_version") or ""),
        str(group.get("benchmark_mode") or "end_to_end"),
        str(group.get("model") or ""),
        str(group.get("embedding_backend") or ""),
        str(group.get("embedding_model") or ""),
        str(group.get("thinking_mode") or "legacy/unrecorded"),
        str(group.get("prompt_version") or ""),
        False,
        round(float(group.get("temperature") or 0), 4),
        str(group.get("answer_style") or ""),
        int(group.get("num_predict") or 0),
    )


def verifier_is_worthwhile(
    group: dict[str, Any],
    groups_by_key: dict[tuple[Any, ...], dict[str, Any]],
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
    groups_by_key: dict[tuple[Any, ...], dict[str, Any]],
) -> dict[str, Any] | None:
    eligible = [
        group
        for group in groups
        if group.get("recommendation_eligible")
        and str(group.get("benchmark_mode") or "") == "end_to_end"
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
            str(group.get("thinking_mode") or "") != "off",
            str(group.get("model") or ""),
        ),
    )[0]


def recommendation_score(group: dict[str, Any]) -> float:
    mean_practical = float(group.get("mean_practical_pass_rate") or group.get("mean_pass_rate") or 0.0)
    worst_practical = float(group.get("worst_run_practical_pass_rate") or 0.0)
    coverage = float(group.get("mean_coverage") or group.get("latest_run_coverage") or 0.0)
    stability_penalty = 0.02 if int(group.get("run_count") or 0) == 1 else 0.0
    timeout_penalty = float(group.get("timeout_rate") or 0.0) * 0.5
    coverage_penalty = max(0.0, 0.95 - coverage) * 0.2
    return mean_practical * 0.75 + worst_practical * 0.25 - stability_penalty - timeout_penalty - coverage_penalty


def recommendation_reason(
    group: dict[str, Any] | None,
    *,
    current_suite_version: str = EVAL_SUITE_VERSION,
    excluded_count: int = 0,
    incomplete_count: int = 0,
) -> str:
    if not group:
        if excluded_count:
            return (
                f"No comparable {current_suite_version} benchmark runs yet. "
                f"{excluded_count} older/incompatible run(s) are excluded from recommendation."
            )
        if incomplete_count:
            return (
                f"No completed eligible {current_suite_version} end-to-end benchmark runs yet. "
                f"{incomplete_count} partial/incomplete run(s) are excluded from recommendation."
            )
        return "No benchmark runs are available yet."
    verifier = "verifier on" if group.get("verify") else "verifier off"
    excluded_note = (
        f" {excluded_count} older/incompatible run(s) are excluded."
        if excluded_count
        else ""
    )
    incomplete_note = (
        f" {incomplete_count} partial/incomplete run(s) are excluded."
        if incomplete_count
        else ""
    )
    return (
        f"Recommended among completed {current_suite_version} end-to-end runs by practical mean/worst-run score, "
        f"retrieval quality, and lower latency: "
        f"{group.get('model')} using {group.get('embedding_backend')}/{group.get('embedding_model')}, "
        f"thinking {group.get('thinking_mode')}, {verifier}, temperature {float(group.get('temperature') or 0):.2f}."
        f"{excluded_note}"
        f"{incomplete_note}"
    )


def guidance_labels_for_group(
    group: dict[str, Any],
    groups_by_key: dict[tuple[Any, ...], dict[str, Any]],
) -> list[str]:
    labels: list[str] = []
    pass_rate = float(group.get("mean_pass_rate") or group.get("latest_run_pass_rate") or 0.0)
    expected_failures = int(group.get("latest_expected_failures") or 0)
    forbidden_failures = int(group.get("latest_forbidden_failures") or 0)
    source_failures = int(group.get("latest_source_failures") or 0)

    labels.append(str(group.get("repeatability_label") or "Provisional"))
    if str(group.get("benchmark_mode") or "") != "end_to_end":
        labels.append(f"{group.get('benchmark_mode')} mode")
    if float(group.get("timeout_rate") or 0.0) >= 0.5:
        labels.append("Timeout dominated")
    if str(group.get("thinking_mode") or "") == "on":
        labels.append("Thinking on")

    if pass_rate >= 0.85 and source_failures == 0 and forbidden_failures == 0:
        labels.append("Recommended for Potato Mode")
    if pass_rate >= 0.4 and forbidden_failures == 0:
        labels.append("Good extraction baseline")
    if int(group.get("chronology_attempted") or 0) >= 2 and float(group.get("chronology_pass_rate") or 0.0) < 0.75:
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
    return finalize_guidance_labels(labels)


def add_fastest_usable_label(groups: list[dict[str, Any]]) -> None:
    eligible = [
        group
        for group in groups
        if group.get("recommendation_eligible")
        and str(group.get("status") or "completed") == "completed"
        and int(group.get("latest_run_total") or 0) > 0
    ]
    if not eligible:
        return
    by_scope: dict[tuple[str, str, str, str], list[dict[str, Any]]] = {}
    for group in eligible:
        by_scope.setdefault(
            (
                str(group.get("suite_version") or ""),
                str(group.get("benchmark_mode") or ""),
                str(group.get("embedding_backend") or ""),
                str(group.get("embedding_model") or ""),
            ),
            [],
        ).append(group)
    for scoped_groups in by_scope.values():
        fastest = sorted(
            scoped_groups,
            key=lambda group: (
                int(group.get("median_avg_latency_ms") or group.get("latest_run_avg_latency_ms") or 0),
                bool(group.get("verify")),
                str(group.get("thinking_mode") or "") != "off",
                str(group.get("model") or ""),
                str(group.get("key") or ""),
            ),
        )[0]
        fastest_latency = max(int(fastest.get("median_avg_latency_ms") or fastest.get("latest_run_avg_latency_ms") or 0), 1)
        tied = [
            group
            for group in scoped_groups
            if abs(int(group.get("median_avg_latency_ms") or group.get("latest_run_avg_latency_ms") or 0) - fastest_latency)
            / fastest_latency
            <= 0.01
        ]
        if len(tied) > 1:
            # Treat <=1% latency differences as noise and avoid declaring a
            # fastest winner when the measured result is effectively tied.
            continue
        fastest["guidance_labels"] = finalize_guidance_labels(["Fastest usable config", *fastest.get("guidance_labels", [])])


def chronology_stats_for_runs(runs: list[dict[str, Any]]) -> dict[str, Any]:
    cases = [
        case
        for run in runs
        for case in run_cases(run)
        if str(case.get("case_category") or "") == "chronology_comprehension"
        and str(case.get("status") or "completed") == "completed"
    ]
    attempted = len(cases)
    passed = sum(1 for case in cases if case.get("passed"))
    return {
        "attempted": attempted,
        "passed": passed,
        "failed": attempted - passed,
        "pass_rate": passed / attempted if attempted else 0.0,
    }


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
        "observation_label": case_observation_label(attempts, passes),
    }


def case_observation_label(attempts: int, passes: int) -> str:
    if attempts < 3:
        if passes == attempts:
            return f"passed in all tested configurations; provisional, {attempts} observation(s)"
        if passes == 0:
            return f"failed in all tested configurations; provisional, {attempts} observation(s)"
        return f"mixed result; provisional, {attempts} observation(s)"
    if passes == attempts:
        return "usually passing"
    if passes == 0:
        return "usually failing"
    return f"mixed result across {attempts} observations"


def score_run_cases(cases: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    by_mode = {
        "retrieval": [case for case in cases if case.get("benchmark_mode") == "retrieval_only"],
        "oracle_generation": [case for case in cases if case.get("benchmark_mode") == "oracle_generation"],
        "end_to_end": [case for case in cases if case.get("benchmark_mode") == "end_to_end"],
    }
    practical = [
        case
        for case in cases
        if case.get("counts_toward_primary") and case.get("case_category") != "negation_adversarial"
    ]
    adversarial = [
        case
        for case in cases
        if case.get("case_category") == "negation_adversarial"
    ]
    return {
        "retrieval": aggregate_score(by_mode["retrieval"]),
        "oracle_generation": aggregate_score(by_mode["oracle_generation"]),
        "end_to_end": aggregate_score(by_mode["end_to_end"]),
        "practical": aggregate_score(practical),
        "adversarial": aggregate_score(adversarial),
    }


def aggregate_score(cases: list[dict[str, Any]]) -> dict[str, Any]:
    total = len(cases)
    passed = sum(1 for case in cases if case.get("passed"))
    taxonomy = pipeline_taxonomy_counts(cases)
    timeout = taxonomy["timeout"]
    errors = taxonomy["runtime_error"]
    grader_review = taxonomy["grader_review"]
    failed = taxonomy["retrieval_only"] + taxonomy["generation_only"] + taxonomy["both"]
    adjudicated_total = passed + failed
    coverage = adjudicated_total / total if total else 0.0
    hit_at1_values = [
        bool((case.get("retrieval_metrics") or {}).get("hit_at1"))
        for case in cases
        if isinstance(case.get("retrieval_metrics"), dict) and case.get("retrieval_metrics")
    ]
    mrr_values = [
        float((case.get("retrieval_metrics") or {}).get("reciprocal_rank") or 0)
        for case in cases
        if isinstance(case.get("retrieval_metrics"), dict) and case.get("retrieval_metrics")
    ]
    return {
        "attempted": total,
        "total": total,
        "passed": passed,
        "failed": failed,
        "grader_review": grader_review,
        "timeout_count": timeout,
        "runtime_error_count": errors,
        "grader_review_count": grader_review,
        "adjudicated_total": adjudicated_total,
        "adjudicated_pass_rate": passed / adjudicated_total if adjudicated_total else 0.0,
        "coverage": coverage,
        "pass_rate": passed / adjudicated_total if adjudicated_total else 0.0,
        "raw_pass_rate": passed / total if total else 0.0,
        "timeout_error_rate": (timeout + errors) / total if total else 0.0,
        "pipeline_taxonomy": taxonomy,
        "retrieval_caused_failures": taxonomy["retrieval_only"],
        "generation_caused_failures": taxonomy["generation_only"],
        "both_failures": taxonomy["both"],
        "hit_at1": sum(1 for value in hit_at1_values if value) / len(hit_at1_values) if hit_at1_values else 0.0,
        "mrr": sum(mrr_values) / len(mrr_values) if mrr_values else 0.0,
    }


def canonical_pipeline_diagnosis(value: str) -> str:
    normalized = (value or "").strip().lower().replace("-", "_").replace(" ", "_")
    if normalized in {"passed", "ok"}:
        return "passed"
    if normalized in {"retrieval_only", "retrieval_failure"} or normalized.startswith("retrieval"):
        return "retrieval_only"
    if normalized in {"generation_only", "generation_comprehension_failure"} or "generation" in normalized:
        return "generation_only"
    if normalized.startswith("both") or "combined" in normalized:
        return "both"
    if "grader" in normalized or "review" in normalized:
        return "grader_review"
    if "timeout" in normalized:
        return "timeout"
    if "runtime" in normalized or "error" in normalized:
        return "runtime_error"
    return "passed" if not normalized else "runtime_error"


def canonical_terminal_diagnosis(
    *,
    status: str = "",
    verifier_status: str = "",
    grader_review_required: bool = False,
    passed: bool = False,
    retrieval_failed: bool = False,
    generation_failed: bool = False,
) -> str:
    clean_status = (status or "").strip().lower()
    clean_verifier_status = (verifier_status or "").strip().lower()
    if clean_status == "timeout" or clean_verifier_status == "timeout":
        return "timeout"
    if clean_status in {"error", "runtime_error"}:
        return "runtime_error"
    if grader_review_required:
        return "grader_review"
    if passed:
        return "passed"
    if retrieval_failed and generation_failed:
        return "both"
    if retrieval_failed:
        return "retrieval_only"
    return "generation_only"


def canonical_case_diagnosis(case: dict[str, Any]) -> str:
    raw = canonical_pipeline_diagnosis(str(case.get("pipeline_diagnosis") or ""))
    status = str(case.get("status") or "").strip().lower()
    if status == "timeout" or raw == "timeout":
        return "timeout"
    if status == "error" or raw == "runtime_error":
        return "runtime_error"
    if case.get("grader_review_required") or raw == "grader_review":
        return "grader_review"
    if case.get("passed"):
        return "passed"
    if raw == "passed" and "passed" not in case:
        return "passed"
    if raw in {"retrieval_only", "generation_only", "both"}:
        return raw
    retrieval_failed = case.get("source_passed") is False
    generation_failed = not (case.get("expected_passed") and case.get("forbidden_passed"))
    return canonical_terminal_diagnosis(
        status=status,
        passed=False,
        retrieval_failed=retrieval_failed,
        generation_failed=generation_failed,
    )


def primary_case_bucket(case: dict[str, Any]) -> str:
    return canonical_case_diagnosis(case)


def pipeline_taxonomy_counts(cases: list[dict[str, Any]]) -> dict[str, int]:
    counts = {bucket: 0 for bucket in PIPELINE_BUCKETS}
    for case in cases:
        category = primary_case_bucket(case)
        counts[category] += 1
    return counts


def dedupe_labels(labels: list[str]) -> list[str]:
    seen: set[str] = set()
    result: list[str] = []
    for label in labels:
        if label in seen:
            continue
        seen.add(label)
        result.append(label)
    return result


def finalize_guidance_labels(labels: list[str]) -> list[str]:
    deduped = dedupe_labels(labels)
    order = {label: index for index, label in enumerate(GUIDANCE_LABEL_ORDER)}
    original = {label: index for index, label in enumerate(deduped)}
    return sorted(deduped, key=lambda label: (order.get(label, len(order)), original[label]))


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


def row_get(row: Any, key: str, default: Any = None) -> Any:
    try:
        if hasattr(row, "keys") and key not in row.keys():
            return default
        value = row[key]
    except Exception:
        if isinstance(row, dict):
            value = row.get(key, default)
        else:
            return default
    return default if value is None else value


def json_loads(value: Any, default: Any) -> Any:
    if value in (None, ""):
        return default
    if isinstance(value, (dict, list)):
        return value
    try:
        return json.loads(str(value))
    except json.JSONDecodeError:
        return default


def safe_name(value: str) -> str:
    return "".join(char if char.isalnum() else "-" for char in value).strip("-") or "model"
