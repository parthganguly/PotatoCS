"""Evaluation-only adapter for run_rag_evals.py; frozen synthetic inputs only.

No retrieval, acquisition, repair, profile reuse, retries, or semantic auto-grading.
Raw requests/replies below are synthetic pilot artifacts, never app diagnostics.
"""
from __future__ import annotations

import hashlib
import ipaddress
import json
import platform
import re
import sys
import tempfile
import time
from dataclasses import asdict
from pathlib import Path
from urllib.parse import urlsplit

import psutil

from odysseus_desktop_backend.services.model_service import ModelService, OLLAMA_ENDPOINT
from odysseus_desktop_backend.services.search_service import (
    EvidencePassage, SearchBudget, SearchMetrics, SearchService, VerifiedEvidence,
    build_evidence_spans, evidence_input_budget_tokens, pack_evidence_window,
    parse_json_object_detailed, resolve_answer_citations, resolve_model_context_tokens,
    untrusted_content_system_prompt, valid_evidence_selection, verify_evidence_span_selection,
)
from odysseus_desktop_backend.storage import Database

ROOT = Path(__file__).resolve().parents[1]
MANIFEST = ROOT / "evals/search_replay/manifest.json"


def schedule(manifest):
    return [
        {"repeat": repeat + 1, "case_id": case["id"], "arm": arm}
        for repeat in range(manifest["repetitions"])
        for index, case in enumerate(manifest["cases"])
        for arm in (("A", "B") if (repeat + index) % 2 == 0 else ("B", "A"))
    ]


def dossier_for(case):
    return [EvidencePassage(
        passage_id=row["id"], source_document_id=row["id"], text=row["text"],
        source_start=0, source_end=len(row["text"]), title=row["title"], source_origin="local",
    ) for row in case["passages"]]


def compact_prompt(question, dossier):
    return (
        "Answer only from the supplied text. Preserve dates, units, negation, exceptions, and other qualifiers. "
        "If the evidence is insufficient, say so explicitly. Cite supporting sentences using source IDs "
        "like [E1]. Never invent a URL.\nQUESTION:\n" + question + "\nSOURCES:\n"
        + "\n\n".join(f"E{i}\nSOURCE_ID={p.source_document_id}\nTITLE={p.title}\nTEXT={p.text}"
                         for i, p in enumerate(dossier, 1))
    )


def citation_mapping(dossier):
    return [VerifiedEvidence(
        evidence_id=f"E{i}", passage_id=p.passage_id, source_document_id=p.source_document_id,
        exact_quote=p.text, quote_start=p.source_start, quote_end=p.source_end,
        title=p.title, source_origin=p.source_origin, canonical_url="", final_url="",
        fetched_at=0, provenance_kind=p.provenance_kind, page_number=None,
    ) for i, p in enumerate(dossier, 1)]


def answer_structure(raw, evidence):
    ids = re.findall(r"\[(E\d+|\d+)\]", raw, re.I)
    allowed = {e.evidence_id.upper() for e in evidence} | {str(i) for i in range(1, len(evidence) + 1)}
    return {"raw_citation_ids": ids, "citation_membership": all(i.upper() in allowed for i in ids),
            "has_raw_citation": bool(ids)}


def resources():
    memory = psutil.virtual_memory()
    processes = []
    for process in psutil.process_iter(["pid", "name", "memory_info"]):
        if "ollama" in (process.info["name"] or "").lower():
            info = process.info["memory_info"]
            processes.append({"pid": process.pid, "name": process.info["name"],
                              "rss": info.rss if info else None})
    return {"available_bytes": memory.available, "total_bytes": memory.total, "ollama": processes}


class Recorder:
    """Capture the actual ModelService boundary without changing its options."""
    def __init__(self, models, deadline):
        self.models, self.deadline = models, deadline
        self.calls = []
        self.initial_loaded = None

    def ps(self):
        return self.models.ps()

    def chat_detailed(self, model, messages, **kwargs):
        remaining = self.deadline - time.monotonic()
        if remaining <= 0 or len(self.calls) >= 54:
            raise TimeoutError("frozen batch limit reached")
        kwargs["timeout"] = min(kwargs["timeout"], remaining)
        call = {"model": model, "messages": messages, "arguments": kwargs}
        self.calls.append(call)
        start = time.monotonic()
        try:
            response = self.models.chat_detailed(model, messages, **kwargs)
            call["response"] = response
            return response
        except Exception as exc:
            call["error_type"] = type(exc).__name__
            raise
        finally:
            call["latency_seconds"] = time.monotonic() - start
            if len(self.calls) == 1:
                self.initial_loaded = self.models.ps()


def replay_trial(case, arm, service, deadline):
    dossier = dossier_for(case)
    metrics, operations, warnings = SearchMetrics(), [], []
    budget = SearchBudget(second_round_enabled=False)
    row = {"case_id": case["id"], "arm": arm}
    if arm == "A":
        prompt = compact_prompt(case["question"], dossier)
        assert all(p.text in prompt for p in dossier)
        evidence = citation_mapping(dossier)
        response = service._model_call(
            "llama3.2:latest", [{"role": "system", "content": untrusted_content_system_prompt()},
                                 {"role": "user", "content": prompt}],
            budget, metrics, deadline, num_predict=1200, response_format=None,
            context_tokens=resolve_model_context_tokens(
                service.models, "llama3.2:latest", budget.model_context_tokens
            )[0],
        )
        raw = response["content"]
        answer = resolve_answer_citations(raw, evidence)
        row.update(json_valid=None, schema_valid=None, pointer_membership=None, exact_resolution=None,
                   all_evidence_visible=True, needs_more_search=False)
    else:
        selected, spans, more, diagnostics = service._select_evidence(
            case["question"], dossier, "llama3.2:latest", budget, metrics, operations, deadline,
            warnings=warnings,
        )
        call = service.models.calls[-1]
        selection_raw = call["response"]["content"]
        parsed, code = parse_json_object_detailed(selection_raw)
        all_spans = build_evidence_spans(dossier)
        visible = {s.span_id for s in spans} == {s.span_id for s in all_spans}
        visible = visible and all(f"SPAN_ID={s.span_id}\n{s.text}" in call["messages"][-1]["content"] for s in all_spans)
        assert visible, "unequal evidence coverage: do not score this trial"
        evidence, verification = verify_evidence_span_selection(selected, spans, dossier, metrics, operations)
        row.update(json_valid=not bool(code), schema_valid=valid_evidence_selection(parsed),
                   pointer_membership=all(s in {p.span_id for p in spans} for s in selected),
                   exact_resolution=all(not d.rejection_code for d in verification),
                   selected=selected, diagnostics=[asdict(d) for d in diagnostics + verification],
                   all_evidence_visible=visible, needs_more_search=more)
        if evidence:
            answer, response, evidence = service._synthesize(case["question"], evidence, "llama3.2:latest",
                                                             budget, metrics, operations, deadline)
            raw = response["content"]
        else:
            answer, raw = "", ""
        if metrics.evidence_selection_fallbacks and answer:
            from odysseus_desktop_backend.services.search_service import DEGRADED_EVIDENCE_NOTE
            answer = DEGRADED_EVIDENCE_NOTE + "\n\n" + answer
    row.update(answer=answer, raw_answer=raw, evidence=[asdict(e) for e in evidence],
               metrics=asdict(metrics), warnings=warnings, **answer_structure(raw, evidence))
    return row


def run_replay(output):
    assert ipaddress.ip_address(urlsplit(OLLAMA_ENDPOINT).hostname).is_loopback
    output.mkdir(parents=True, exist_ok=False)
    manifest_bytes = MANIFEST.read_bytes()
    manifest = json.loads(manifest_bytes)
    order = schedule(manifest)
    (output / "manifest.json").write_bytes(manifest_bytes)
    (output / "order.json").write_text(json.dumps(order, indent=2), encoding="utf-8")
    hashes = {"manifest_sha256": hashlib.sha256(manifest_bytes).hexdigest(),
              "order_sha256": hashlib.sha256((output / "order.json").read_bytes()).hexdigest(),
              "adapter_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest()}
    (output / "frozen.json").write_text(json.dumps(hashes, indent=2), encoding="utf-8")
    with tempfile.TemporaryDirectory(prefix="potatocs-search-replay-") as temp:
        db = Database(Path(temp))
        try:
            models = ModelService(db)
            status, residency = models.detect_ollama(), models.ps()
            identity = next((m for m in status["model_details"] if m["name"] == manifest["model"]), None)
            provenance = {**hashes, "endpoint": OLLAMA_ENDPOINT, "identity": identity,
                          "ollama_version": status["version"], "initial_residency": residency,
                          "python": sys.executable, "python_version": sys.version, "platform": platform.platform(),
                          "source": sys.modules[SearchService.__module__].__file__, "initial_resources": resources()}
            if identity is None or identity.get("format") != "gguf":
                provenance["status"] = "NOT RUN: required local GGUF model unavailable"
                (output / "provenance.json").write_text(json.dumps(provenance, indent=2), encoding="utf-8")
                return 2
            provenance["capabilities"] = models.inspect(manifest["model"])
            context, source = resolve_model_context_tokens(models, manifest["model"])
            provenance.update(budget_context=context, budget_context_source=source)
            for case in manifest["cases"]:
                spans = build_evidence_spans(dossier_for(case))
                window = pack_evidence_window(case["question"], spans, [], input_budget_tokens=evidence_input_budget_tokens(context))
                assert window.spans == spans, "manifest evidence exceeds production window"
            (output / "provenance.json").write_text(json.dumps(provenance, indent=2), encoding="utf-8")
            started = time.monotonic()
            recorder = Recorder(models, started + 1800)
            service = SearchService.__new__(SearchService)
            service.models = recorder
            cases = {c["id"]: c for c in manifest["cases"]}
            completed = 0
            with (output / "trials.jsonl").open("x", encoding="utf-8") as stream:
                for trial in order:
                    if time.monotonic() >= recorder.deadline:
                        break
                    call_start, arm_start = len(recorder.calls), time.monotonic()
                    row = dict(trial)
                    try:
                        row.update(replay_trial(cases[trial["case_id"]], trial["arm"], service,
                                                min(recorder.deadline, arm_start + 90)))
                        row["status"] = "completed"
                    except Exception as exc:
                        row.update(status="failed", error_type=type(exc).__name__)
                    row.update(calls=recorder.calls[call_start:], latency_seconds=time.monotonic() - arm_start)
                    stream.write(json.dumps(row, ensure_ascii=False) + "\n")
                    stream.flush()
                    completed += 1
                    print(f"{completed}/36 {trial['case_id']} {trial['arm']} {row['status']}", flush=True)
            provenance.update(attempts=completed, generation_calls=len(recorder.calls),
                              elapsed_seconds=time.monotonic() - started, first_call_residency=recorder.initial_loaded,
                              final_residency=models.ps(), final_resources=resources())
            (output / "provenance.json").write_text(json.dumps(provenance, indent=2), encoding="utf-8")
            return 0 if completed == 36 else 2
        finally:
            db.close()
