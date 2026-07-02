from __future__ import annotations

import json
import re
from dataclasses import asdict, dataclass, field
from typing import Any


VISION_BACKEND_AUTOMATIC = "automatic"
VISION_BACKEND_FLORENCE2 = "florence2"
VISION_BACKEND_OLLAMA = "ollama"
VISION_BACKEND_OCR_ONLY = "ocr_only"
VISION_BACKENDS = {
    VISION_BACKEND_AUTOMATIC,
    VISION_BACKEND_FLORENCE2,
    VISION_BACKEND_OLLAMA,
    VISION_BACKEND_OCR_ONLY,
}


FLORENCE_TASK_CAPTION = "caption"
FLORENCE_TASK_DETAILED_CAPTION = "detailed_caption"
FLORENCE_TASK_MORE_DETAILED_CAPTION = "more_detailed_caption"
FLORENCE_TASK_OBJECT_DETECTION = "object_detection"
FLORENCE_TASK_DENSE_REGION_CAPTION = "dense_region_caption"
FLORENCE_TASK_PHRASE_GROUNDING = "phrase_grounding"
FLORENCE_TASK_OPEN_VOCABULARY_DETECTION = "open_vocabulary_detection"
FLORENCE_TASK_OCR = "ocr"
FLORENCE_TASK_OCR_WITH_REGION = "ocr_with_region"


@dataclass
class PerceptionTaskPlan:
    tasks: list[str]
    target_phrases: list[str] = field(default_factory=list)
    needs_exact_text: bool = False
    needs_visual_details: bool = True
    reason: str = "general_visual_question"

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def normalize_vision_backend(value: str | None) -> str:
    cleaned = (value or VISION_BACKEND_AUTOMATIC).strip().lower()
    aliases = {
        "florence": VISION_BACKEND_FLORENCE2,
        "florence_2": VISION_BACKEND_FLORENCE2,
        "florence-2": VISION_BACKEND_FLORENCE2,
        "basic": VISION_BACKEND_FLORENCE2,
        "basic_local": VISION_BACKEND_FLORENCE2,
        "basic_local_vision": VISION_BACKEND_FLORENCE2,
        "enhanced": VISION_BACKEND_OLLAMA,
        "ollama_vision": VISION_BACKEND_OLLAMA,
        "ocr": VISION_BACKEND_OCR_ONLY,
    }
    cleaned = aliases.get(cleaned, cleaned)
    if cleaned not in VISION_BACKENDS:
        raise ValueError("vision_backend must be automatic, florence2, ollama, or ocr_only")
    return cleaned


def plan_perception_tasks(question: str | None, mode: str | None = "automatic") -> PerceptionTaskPlan:
    text = (question or "").strip().lower()
    requested_mode = (mode or "automatic").strip()
    if requested_mode == "ocr_only":
        return PerceptionTaskPlan(
            tasks=[FLORENCE_TASK_OCR_WITH_REGION],
            needs_exact_text=True,
            needs_visual_details=False,
            reason="ocr_only_mode",
        )

    target_phrases = extract_target_phrases(text)
    asks_text = bool(re.search(r"\b(text|read|ocr|words?|letters?|sign|label|caption|serial|code|number)\b", text))
    asks_presence = bool(re.search(r"\b(is there|are there|do you see|can you see|does it show|visible)\b", text))
    asks_location = bool(re.search(r"\b(where|location|position|left|right|above|below|behind|next to|near)\b", text))
    asks_identity = bool(re.search(r"\b(who|which person|name|brand|logo|place|landmark|event|date|company)\b", text))
    asks_scene = bool(re.search(r"\b(describe|tell me about|what is in|what's in|image|photo|picture|screenshot|scene|desk|object|layout)\b", text))

    tasks: list[str] = []
    reason = "general_visual_question"
    if asks_text:
        tasks.append(FLORENCE_TASK_OCR_WITH_REGION)
        reason = "text_question"
    if asks_presence and target_phrases:
        tasks.extend([FLORENCE_TASK_OPEN_VOCABULARY_DETECTION, FLORENCE_TASK_MORE_DETAILED_CAPTION])
        reason = "presence_or_open_vocabulary_question"
    elif asks_location or asks_scene:
        tasks.extend([FLORENCE_TASK_MORE_DETAILED_CAPTION, FLORENCE_TASK_DENSE_REGION_CAPTION, FLORENCE_TASK_OBJECT_DETECTION])
        reason = "scene_or_layout_question"
    elif asks_identity:
        tasks.extend([FLORENCE_TASK_MORE_DETAILED_CAPTION, FLORENCE_TASK_OBJECT_DETECTION])
        reason = "identity_sensitive_question"
    else:
        tasks.extend([FLORENCE_TASK_MORE_DETAILED_CAPTION, FLORENCE_TASK_OBJECT_DETECTION])

    return PerceptionTaskPlan(
        tasks=dedupe_preserve_order(tasks),
        target_phrases=target_phrases,
        needs_exact_text=asks_text,
        needs_visual_details=True,
        reason=reason,
    )


def extract_target_phrases(text: str) -> list[str]:
    phrases: list[str] = []
    for pattern in (
        r"\b(?:is there|are there|do you see|can you see|does it show)\s+(?:an?|the)?\s*([a-z0-9][a-z0-9\- ]{1,48})",
        r"\b(?:where is|where are)\s+(?:an?|the)?\s*([a-z0-9][a-z0-9\- ]{1,48})",
        r"\b(?:about|of)\s+(?:an?|the)?\s*([a-z0-9][a-z0-9\- ]{1,48})",
    ):
        for match in re.finditer(pattern, text):
            phrase = clean_phrase(match.group(1))
            if phrase:
                phrases.append(phrase)
    return dedupe_preserve_order(phrases)[:5]


def clean_phrase(value: str) -> str:
    cleaned = re.sub(r"\b(in|on|at|with|near|from|to|and|or|please|image|photo|picture|screenshot)\b.*$", "", value)
    return " ".join(cleaned.strip(" ?.!,:;\"'").split())[:60]


def normalize_box(box: Any, width: int, height: int) -> list[float]:
    values = [float(item) for item in list(box or [])[:4]]
    while len(values) < 4:
        values.append(0.0)
    x1, y1, x2, y2 = values
    if max(abs(x1), abs(y1), abs(x2), abs(y2)) > 1.5:
        safe_width = max(int(width or 0), 1)
        safe_height = max(int(height or 0), 1)
        x1, x2 = x1 / safe_width, x2 / safe_width
        y1, y2 = y1 / safe_height, y2 / safe_height
    left = clamp01(min(x1, x2))
    top = clamp01(min(y1, y2))
    right = clamp01(max(x1, x2))
    bottom = clamp01(max(y1, y2))
    return [round(left, 6), round(top, 6), round(right, 6), round(bottom, 6)]


def normalize_quad(quad: Any, width: int, height: int) -> list[list[float]]:
    values = [float(item) for item in list(quad or [])[:8]]
    while len(values) < 8:
        values.append(0.0)
    points: list[list[float]] = []
    for index in range(0, 8, 2):
        x_value = values[index]
        y_value = values[index + 1]
        if max(abs(item) for item in values) > 1.5:
            x_value = x_value / max(int(width or 0), 1)
            y_value = y_value / max(int(height or 0), 1)
        points.append([round(clamp01(x_value), 6), round(clamp01(y_value), 6)])
    return points


def normalize_florence_outputs(
    raw_by_task: dict[str, Any],
    *,
    model: str,
    question: str,
    plan: PerceptionTaskPlan | dict[str, Any],
    image_width: int,
    image_height: int,
    elapsed_ms: int = 0,
) -> dict[str, Any]:
    plan_dict = plan.to_dict() if isinstance(plan, PerceptionTaskPlan) else dict(plan)
    tasks = [str(item) for item in plan_dict.get("tasks") or []]
    summary_parts: list[str] = []
    objects: list[dict[str, Any]] = []
    regions: list[dict[str, Any]] = []
    text_items: list[dict[str, Any]] = []
    grounded_phrases: list[dict[str, Any]] = []
    uncertain: list[str] = []
    not_determinable: list[str] = []

    for task, payload in raw_by_task.items():
        parsed = unwrap_florence_payload(payload)
        if isinstance(parsed, str) and parsed.strip():
            if task in {FLORENCE_TASK_OCR, FLORENCE_TASK_OCR_WITH_REGION}:
                text_items.append({"text": parsed.strip(), "source": task, "confidence": None})
            else:
                summary_parts.append(parsed.strip())
            continue
        if not isinstance(parsed, dict):
            continue
        labels = list(parsed.get("labels") or parsed.get("captions") or parsed.get("phrases") or [])
        boxes = list(parsed.get("bboxes") or parsed.get("boxes") or [])
        quad_boxes = list(parsed.get("quad_boxes") or parsed.get("quadboxes") or [])
        if task in {FLORENCE_TASK_OBJECT_DETECTION, FLORENCE_TASK_OPEN_VOCABULARY_DETECTION}:
            for label, box in zip(labels, boxes):
                objects.append({
                    "label": str(label),
                    "box": normalize_box(box, image_width, image_height),
                    "source": task,
                    "confidence": None,
                })
        elif task == FLORENCE_TASK_DENSE_REGION_CAPTION:
            for label, box in zip(labels, boxes):
                regions.append({
                    "caption": str(label),
                    "box": normalize_box(box, image_width, image_height),
                    "source": task,
                })
        elif task in {FLORENCE_TASK_OCR, FLORENCE_TASK_OCR_WITH_REGION}:
            for label, quad in zip(labels, quad_boxes or boxes):
                item: dict[str, Any] = {"text": str(label), "source": task, "confidence": None}
                if quad_boxes:
                    item["quad"] = normalize_quad(quad, image_width, image_height)
                else:
                    item["box"] = normalize_box(quad, image_width, image_height)
                text_items.append(item)
        elif task == FLORENCE_TASK_PHRASE_GROUNDING:
            for label, box in zip(labels, boxes):
                grounded_phrases.append({
                    "phrase": str(label),
                    "box": normalize_box(box, image_width, image_height),
                    "source": task,
                })
        else:
            caption = parsed.get("caption") or parsed.get("text") or parsed.get(task) or ""
            if caption:
                summary_parts.append(str(caption).strip())

    summary = first_non_empty(summary_parts)
    if plan_dict.get("reason") == "identity_sensitive_question":
        not_determinable.append(
            "Identity, name, biography, location, event, date, or brand claims require explicit visible evidence and may be unavailable."
        )
    if not summary and not objects and not regions and not text_items:
        uncertain.append("Florence-2 returned no structured visual evidence.")
    return {
        "schema": "odysseus.visual_evidence.v1",
        "backend": VISION_BACKEND_FLORENCE2,
        "model": model,
        "question": question,
        "tasks": tasks,
        "task_plan": plan_dict,
        "image": {"width": int(image_width or 0), "height": int(image_height or 0)},
        "summary": summary,
        "objects": objects[:80],
        "regions": regions[:80],
        "text": text_items[:80],
        "grounded_phrases": grounded_phrases[:40],
        "uncertain": uncertain[:40],
        "not_determinable": not_determinable[:40],
        "elapsed_ms": int(elapsed_ms or 0),
        "raw_task_count": len(raw_by_task),
    }


def visual_evidence_to_legacy_observations(evidence: dict[str, Any]) -> dict[str, Any]:
    summary = str(evidence.get("summary") or "")
    objects = [str(item.get("label") or item.get("caption") or "") for item in evidence.get("objects") or []]
    regions = [str(item.get("caption") or "") for item in evidence.get("regions") or []]
    text_items = [str(item.get("text") or "") for item in evidence.get("text") or []]
    return {
        "summary": summary,
        "visible_objects": [item for item in objects if item],
        "spatial_relations": [item for item in regions if item],
        "interface_elements": [],
        "uncertain_observations": [str(item) for item in evidence.get("uncertain") or []],
        "not_visible_or_not_determinable": [str(item) for item in evidence.get("not_determinable") or []],
        "model_visible_text": [item for item in text_items if item],
    }


def visual_evidence_prompt_json(evidence: dict[str, Any]) -> str:
    public = {
        key: evidence.get(key)
        for key in (
            "schema",
            "backend",
            "model",
            "question",
            "tasks",
            "task_plan",
            "image",
            "summary",
            "objects",
            "regions",
            "text",
            "grounded_phrases",
            "uncertain",
            "not_determinable",
        )
    }
    return json.dumps(public, ensure_ascii=False, indent=2)


def strict_visual_synthesis_rules() -> str:
    return (
        "Evidence rules: the final text model did not inspect the pixels unless native image request is yes. "
        "Treat only OCR text and structured visual evidence as evidence. Previous assistant claims are not evidence. "
        "Do not invent identity, name, biography, location, event, date, brand, motive, emotion, nationality, "
        "relationship, or hidden context. If a requested fact is not explicitly supported, say it cannot be determined "
        "from the available image evidence."
    )


def opinion_or_evaluation_synthesis_rules() -> str:
    return (
        "Opinion/evaluation rules: when the user asks for an opinion or evaluation about visible image content, "
        "answer in four clearly labeled parts: Visible facts, Reasonable visual interpretation, Opinion, and Limits. "
        "State only image/OCR-supported facts in Visible facts. Explain apparent meaning without claiming hidden intent "
        "in Reasonable visual interpretation. Give the requested judgment in Opinion; do not refuse merely because the "
        "image contains no explicit opinion text. In Limits, identify what the image alone cannot establish, including "
        "manufacturer or designer intent, exact jurisdiction, measured effectiveness, statistics, and real-world outcomes."
    )


def clamp01(value: float) -> float:
    return max(0.0, min(1.0, value))


def dedupe_preserve_order(items: list[str]) -> list[str]:
    seen: set[str] = set()
    result: list[str] = []
    for item in items:
        clean = str(item or "").strip()
        if clean and clean not in seen:
            seen.add(clean)
            result.append(clean)
    return result


def first_non_empty(items: list[str]) -> str:
    for item in items:
        clean = str(item or "").strip()
        if clean:
            return clean
    return ""


def unwrap_florence_payload(payload: Any) -> Any:
    if isinstance(payload, dict) and len(payload) == 1:
        key = next(iter(payload.keys()))
        if str(key).startswith("<") and str(key).endswith(">"):
            return payload[key]
    return payload
