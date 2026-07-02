from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from odysseus_desktop_backend.services.chat_service import ChatService, PLAIN_CHAT_SYSTEM_PROMPT
from odysseus_desktop_backend.services.model_service import ModelService
from odysseus_desktop_backend.services.session_service import SessionService
from odysseus_desktop_backend.services.settings_service import SettingsService
from odysseus_desktop_backend.services.visual_evidence_curator import (
    classify_question,
    compact_synthesis_packet,
    curate_visual_evidence,
    guard_visual_answer,
)
from odysseus_desktop_backend.storage import Database


class CapturingGeneralModelService(ModelService):
    def __init__(self, db: Database):
        super().__init__(db)
        self.calls: list[dict[str, Any]] = []

    def chat_detailed(
        self,
        model: str,
        messages: list[dict[str, str]],
        **kwargs: Any,
    ) -> dict[str, Any]:
        self.calls.append({"model": model, "messages": messages, "kwargs": kwargs})
        question = str(messages[-1].get("content") or "").lower()
        if "eastern european" in question:
            content = (
                "Definitions vary by convention, but commonly listed Eastern European countries include Belarus, "
                "Bulgaria, Czechia, Hungary, Moldova, Poland, Romania, Russia, Slovakia, and Ukraine."
            )
        elif "17 * 19" in question:
            content = "17 × 19 = 323."
        elif "leaves look green" in question:
            content = "Leaves look green because chlorophyll absorbs mostly red and blue light and reflects more green light."
        else:
            content = "A direct general-knowledge answer."
        return {
            "model": model,
            "content": content,
            "thinking": "",
            "done_reason": "stop",
            "total_duration_ns": 1_000_000,
            "load_duration_ns": 0,
            "prompt_eval_count": 20,
            "prompt_eval_duration_ns": 100_000,
            "eval_count": 10,
            "eval_duration_ns": 500_000,
            "elapsed_ms": 4,
            "raw": {},
        }


def build_plain_chat(tmp_path: Path) -> tuple[Database, ChatService, CapturingGeneralModelService]:
    db = Database(tmp_path / "profile")
    models = CapturingGeneralModelService(db)
    chat = ChatService(SessionService(db), SettingsService(db), models)
    return db, chat, models


def assert_plain_chat_steering(call: dict[str, Any]) -> None:
    messages = call["messages"]
    assert messages[0] == {"role": "system", "content": PLAIN_CHAT_SYSTEM_PROMPT}
    assert "answer directly and helpfully using general knowledge" in PLAIN_CHAT_SYSTEM_PROMPT
    assert "Do not refuse simple factual, explanatory, or arithmetic questions" in PLAIN_CHAT_SYSTEM_PROMPT
    assert "avoid pretending to have checked external sources" in PLAIN_CHAT_SYSTEM_PROMPT


def test_plain_chat_eastern_europe_question_is_steered_without_sources(tmp_path: Path):
    db, chat, models = build_plain_chat(tmp_path)
    try:
        result = chat.send("name the eastern european countries")

        assert_plain_chat_steering(models.calls[-1])
        answer = result["assistant_message"]["content"].lower()
        assert "definitions vary" in answer
        assert "ukraine" in answer
        assert "no local sources" not in answer
        trace = result["assistant_message"]["metadata"]["operation_trace"]
        assert trace["pipeline"]["rag_enabled"] is False
        assert PLAIN_CHAT_SYSTEM_PROMPT not in json.dumps(trace)
    finally:
        db.close()


def test_plain_chat_arithmetic_question_answers_directly(tmp_path: Path):
    db, chat, models = build_plain_chat(tmp_path)
    try:
        result = chat.send("what is 17 * 19?")

        assert_plain_chat_steering(models.calls[-1])
        assert "323" in result["assistant_message"]["content"]
    finally:
        db.close()


def test_plain_chat_explanation_uses_general_knowledge_without_rag(tmp_path: Path):
    db, chat, models = build_plain_chat(tmp_path)
    try:
        result = chat.send("explain why leaves look green")

        assert_plain_chat_steering(models.calls[-1])
        answer = result["assistant_message"]["content"].lower()
        assert "chlorophyll" in answer
        assert "green light" in answer
        assert result["grounding"]["verifier"]["status"] == "no_evidence"
    finally:
        db.close()


def tobacco_warning_evidence() -> dict[str, Any]:
    return {
        "schema": "odysseus.visual_evidence.v1",
        "artifact_id": "tobacco-pack",
        "summary": "A tobacco or cigarette pack displays a graphic health warning.",
        "objects": [
            {"label": "tobacco pack", "source": "object_detection"},
            {"label": "warning label", "source": "dense_region_caption"},
        ],
        "regions": [
            {"caption": "A graphic warning image appears on the pack.", "source": "dense_region_caption"}
        ],
        "text": [{"text": "TOBACCO CAUSES PAINFUL DEATH", "source": "ocr"}],
        "grounded_phrases": [],
        "uncertain": [],
        "not_determinable": [],
    }


def opinion_prompt(question: str) -> tuple[dict[str, Any], str, str]:
    curated = curate_visual_evidence(tobacco_warning_evidence(), question)
    packet = compact_synthesis_packet(
        question,
        curated,
        ocr_text="TOBACCO CAUSES PAINFUL DEATH",
    )
    analysis = {
        "mode": "combined",
        "actual_vision_backend": "florence2",
        "output": {
            "ocr_text": "TOBACCO CAUSES PAINFUL DEATH",
            "visual_evidence": tobacco_warning_evidence(),
            "curated_visual_evidence": curated,
            "provenance": {"ocr_engine": "tesseract", "vision_backend": "florence2"},
        },
    }
    route = {"mode_requested": "automatic", "mode_executed": "combined", "vision_backend": "florence2"}
    chat = object.__new__(ChatService)
    prompt = chat._multimodal_synthesis_prompt(question, analysis, route)
    return curated, packet, prompt


def test_visual_opinion_question_gets_fact_interpretation_opinion_and_limits_rules():
    question = "what is your opinion on this sort of advertising cigarette packs?"
    curated, packet, prompt = opinion_prompt(question)

    assert curated["question_type"] == "opinion_or_evaluation"
    assert "Question type: opinion_or_evaluation" in packet
    for label in ("Visible facts", "Reasonable visual interpretation", "Opinion", "Limits"):
        assert label in prompt
    assert "do not refuse merely because" in prompt.lower()
    assert "manufacturer or designer intent" in prompt
    safe_answer = (
        "Visible facts: A tobacco pack shows a graphic warning and the text TOBACCO CAUSES PAINFUL DEATH. "
        "Reasonable visual interpretation: The warning appears designed to be stark. "
        "Opinion: In my view, the direct wording is forceful and attention-grabbing. "
        "Limits: The image alone cannot establish intent or measured effectiveness."
    )
    assert guard_visual_answer(safe_answer, curated)["ok"] is True


def test_visual_opinion_packet_preserves_exact_ocr_text():
    _curated, packet, prompt = opinion_prompt("Is this warning useful advertising?")

    assert "OCR TEXT\nTOBACCO CAUSES PAINFUL DEATH" in packet
    assert "Exact OCR" not in packet
    assert "TOBACCO CAUSES PAINFUL DEATH" in prompt


def test_truly_not_visible_visual_request_keeps_strict_abstention_rules():
    question = "Did this ad reduce smoking rates?"
    curated, _packet, prompt = opinion_prompt(question)

    assert classify_question(question) != "opinion_or_evaluation"
    assert curated["question_type"] != "opinion_or_evaluation"
    assert "If a requested fact is not explicitly supported, say it cannot be determined" in prompt
    assert "four clearly labeled parts" not in prompt


def test_visual_opinion_wording_forbids_hidden_intent_and_effectiveness_claims():
    opinion_questions = (
        "what do you think of this packaging?",
        "is this good advertising?",
        "is this design effective?",
        "is this warning scary?",
        "judge this poster",
    )
    for question in opinion_questions:
        assert classify_question(question) == "opinion_or_evaluation"

    assert classify_question("Who designed this package?") != "opinion_or_evaluation"
    assert classify_question("What brand is hidden under the blur?") == "brand_or_origin"
    _curated, packet, _prompt = opinion_prompt("is this design effective?")
    assert "Do not claim hidden intent, manufacturer or designer intent" in packet
    assert "measured effectiveness, statistics, or real-world outcomes" in packet


def test_native_visual_opinion_prompt_uses_the_same_safe_answer_shape():
    chat = object.__new__(ChatService)
    prompt = chat._native_multimodal_prompt(
        "what do you think of this packaging?",
        {"output": {"ocr_text": "TOBACCO CAUSES PAINFUL DEATH"}},
        {"context_evidence_action": "active"},
    )

    for label in ("Visible facts", "Reasonable visual interpretation", "Opinion", "Limits"):
        assert label in prompt
    assert "manufacturer or designer intent" in prompt


def test_visual_opinion_operation_trace_keeps_metadata_but_not_private_content(tmp_path: Path):
    db, chat, _models = build_plain_chat(tmp_path)
    try:
        trace = chat.build_operation_trace(
            model_response={
                "model": "llama3.2",
                "content": "RAW-OPINION-RESPONSE",
                "thinking": "FULL-THINKING-TEXT",
                "prompt_eval_count": 30,
                "eval_count": 15,
                "elapsed_ms": 50,
                "raw": {"prompt": "RAW-VISUAL-OPINION-PROMPT"},
            },
            analysis={
                "actual_vision_backend": "florence2",
                "actual_vision_model": "microsoft/Florence-2-base-ft",
                "ocr_engine": "tesseract",
                "output": {
                    "ocr_text": "TOBACCO CAUSES PAINFUL DEATH",
                    "curated_visual_evidence": {
                        "question_type": "opinion_or_evaluation",
                        "direct_observations": [{"text": "PRIVATE-VISIBLE-FACT"}],
                    },
                    "provenance": {"mode_requested": "automatic", "mode_executed": "combined"},
                },
            },
            grounding={"verifier": {"enabled": False, "status": "not_run"}},
            timings={"answer_latency_ms": 50},
            rag_enabled=False,
            verifier_enabled=False,
            retrieved_snippets=[],
            selected_model="llama3.2",
            selected_rag_preset="standard",
            selected_thinking_mode="auto",
            answer_style="precise",
            embedding_model="",
            embedding_backend="",
        )
        serialized = json.dumps(trace)

        assert trace["models"]["vision_backend"] == "florence2"
        assert trace["pipeline"]["executed_multimodal_mode"] == "combined"
        for private_text in (
            "RAW-OPINION-RESPONSE",
            "FULL-THINKING-TEXT",
            "RAW-VISUAL-OPINION-PROMPT",
            "TOBACCO CAUSES PAINFUL DEATH",
            "PRIVATE-VISIBLE-FACT",
        ):
            assert private_text not in serialized
    finally:
        db.close()
