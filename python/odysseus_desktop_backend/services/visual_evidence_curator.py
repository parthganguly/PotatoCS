from __future__ import annotations

import hashlib
import re
from typing import Any


OCR_NO_TEXT_WARNING = "OCR ran, but no text was extracted."
VISUAL_EVIDENCE_CURATOR_VERSION = "visual-evidence-curator-v3"
VISUAL_EVIDENCE_RETRIEVER_VERSION = "visual-evidence-retriever-v1"

VISUAL_SNIPPET_KINDS = {
    "caption",
    "dense_region_caption",
    "object",
    "relation",
    "ocr_text",
    "derived_observation",
    "supported_inference",
}

QUESTION_CLASSES = {
    "scene_description",
    "object_presence",
    "object_identity",
    "object_count",
    "object_attribute",
    "color_attribute",
    "clothing_attribute",
    "spatial_relation",
    "holding_or_contact",
    "action",
    "object_use",
    "source_or_cause",
    "text_reading",
    "brand_or_origin",
    "person_identity",
    "emotion_or_intent",
    "opinion_or_evaluation",
    "comparison",
    "other",
}

LABEL_ALIASES = {
    "bulb": "light bulb",
    "lightbulb": "light bulb",
    "desk lamp": "lamp",
    "table lamp": "lamp",
    "picture": "picture frame",
    "pictures": "picture frame",
    "framed picture": "picture frame",
    "framed pictures": "picture frame",
    "picture frames": "picture frame",
    "photograph": "picture frame",
    "photographs": "picture frame",
    "books": "book",
    "chairs": "chair",
    "houseplant": "plant",
    "houseplants": "plant",
    "plants": "plant",
    "sneakers": "shoe",
    "shoes": "shoe",
    "windows": "window",
    "lamps": "lamp",
    "desks": "desk",
    "vegetables": "vegetable",
    "veggies": "vegetable",
    "cucumbers": "cucumber",
    "knives": "knife",
    "cutting boards": "cutting board",
    "men": "person",
    "man": "person",
    "woman": "person",
    "women": "person",
    "people": "person",
}

LIGHT_SOURCE_ENTITIES = {
    "lamp",
    "light",
    "light bulb",
    "window",
    "sun",
    "sunlight",
    "fire",
    "candle",
    "screen",
    "reflection",
    "shadow",
}

LIGHT_CONTEXT_EXCLUSION_TERMS = {
    "book",
    "cabinet",
    "carpet",
    "chair",
    "curtain",
    "frame",
    "hat",
    "picture",
    "plant",
    "shelf",
    "shoe",
}

ACTION_QUESTION_TYPES = {"action", "holding_or_contact", "object_use"}
ATTRIBUTE_QUESTION_TYPES = {"object_attribute", "color_attribute", "clothing_attribute"}

CLOTHING_TERMS = {
    "clothes", "clothing", "coat", "dress", "jacket", "outerwear", "pants",
    "shirt", "shorts", "skirt", "suit", "sweater", "trousers",
}

COLOR_TERMS = {
    "black", "blue", "brown", "gray", "green", "grey", "orange", "pink",
    "purple", "red", "white", "yellow",
}

PERSON_TERMS = {
    "person",
    "people",
    "man",
    "woman",
    "child",
    "boy",
    "girl",
    "he",
    "she",
    "they",
    "someone",
}

ACTION_RELEVANT_ENTITIES = {
    "book",
    "chair",
    "desk",
    "kitchen",
    "laptop",
    "phone",
    "cup",
    "tool",
    "food",
    "vegetable",
    "cucumber",
    "knife",
    "cutting board",
    "bicycle",
    "car",
    "instrument",
    "hand",
}

ACTION_VERB_ALIASES = {
    "sitting": "sit",
    "seated": "sit",
    "sit": "sit",
    "standing": "stand",
    "stand": "stand",
    "walking": "walk",
    "walk": "walk",
    "running": "run",
    "run": "run",
    "moving": "move",
    "move": "move",
    "reading": "read",
    "read": "read",
    "holding": "hold",
    "held": "hold",
    "hold": "hold",
    "using": "use",
    "used": "use",
    "use": "use",
    "typing": "type",
    "type": "type",
    "cooking": "cook",
    "cook": "cook",
    "driving": "drive",
    "drive": "drive",
    "talking": "talk",
    "talk": "talk",
    "sleeping": "sleep",
    "sleep": "sleep",
    "eating": "eat",
    "eat": "eat",
    "riding": "ride",
    "ride": "ride",
    "writing": "write",
    "write": "write",
    "cutting": "cut",
    "cut": "cut",
    "chopping": "cut",
    "chop": "cut",
    "slicing": "cut",
    "slice": "cut",
    "preparing": "prepare",
    "prepare": "prepare",
    "fighting": "fight",
    "fight": "fight",
    "arguing": "argue",
    "argue": "argue",
}

UNSUPPORTED_ACTION_GUARD_VERBS = {
    "argue",
    "cut",
    "walk",
    "run",
    "stand",
    "move",
    "fight",
    "cook",
    "drive",
    "talk",
    "sleep",
    "eat",
    "hold",
    "type",
}

STOPWORDS = {
    "a",
    "an",
    "and",
    "are",
    "as",
    "at",
    "be",
    "coming",
    "does",
    "from",
    "in",
    "into",
    "is",
    "it",
    "look",
    "looks",
    "of",
    "on",
    "or",
    "the",
    "there",
    "this",
    "to",
    "what",
    "where",
    "which",
    "who",
    "with",
}

UNSUPPORTED_IDENTITY_TEXT = (
    "Identity, biography, event, location, brand, nationality, motive, and emotion claims are not supported "
    "unless directly visible in the evidence."
)


def canonicalize_label(label: str) -> str:
    clean = re.sub(r"\s+", " ", str(label or "").strip().lower())
    clean = clean.strip(" .,:;!?\"'()[]{}")
    clean = LABEL_ALIASES.get(clean, clean)
    if clean.endswith("s") and clean not in {"glasses"}:
        singular = clean[:-1]
        if singular in LABEL_ALIASES:
            clean = LABEL_ALIASES[singular]
    return clean


def normalize_question_text(question: str) -> str:
    q = str(question or "").strip().lower()
    q = q.replace("\u2019", "'").replace("\u2018", "'")
    q = re.sub(r"\bwhat\s*'\s*s\b", "what is", q)
    q = re.sub(r"\bwhat's\b", "what is", q)
    q = re.sub(r"\bwhats\b", "what is", q)
    q = re.sub(r"\bwho\s*'\s*s\b", "who is", q)
    q = re.sub(r"\bwho's\b", "who is", q)
    q = re.sub(r"\bwho\s+is\b", "who is", q)
    q = re.sub(r"[^a-z0-9]+", " ", q)
    return re.sub(r"\s+", " ", q).strip()


def actor_terms_for_question(question: str) -> set[str]:
    q = normalize_question_text(question)
    actors: set[str] = set()
    if re.search(r"\b(man|men|boy|him|he)\b", q):
        actors.add("man")
    if re.search(r"\b(woman|women|girl|her|she)\b", q):
        actors.add("woman")
    if re.search(r"\b(they|them|people|persons)\b", q):
        actors.add("people")
    if re.search(r"\b(person|someone|this person)\b", q):
        actors.add("person")
    return actors


def question_mentions_fighting(question: str) -> bool:
    return bool(re.search(r"\b(fighting|fight|arguing|argue|argument|confrontation)\b", normalize_question_text(question)))


def is_action_intent_question(question: str) -> bool:
    q = normalize_question_text(question)
    actor = r"(?:(?:the\s+)?(?:man|woman|person|child|boy|girl)|this\s+person|he|she|they)"
    actor_context = r"(?:\s+in\s+(?:the\s+)?(?:image|picture|photo))?"
    return any(
        re.search(pattern, q)
        for pattern in (
            rf"\bwhat\s+(?:is|are)\s+{actor}{actor_context}\s+doing\b",
            rf"\bwhat\s+(?:is|are)\s+{actor}{actor_context}\s+up\s+to\b",
            r"\bwhat\s+is\s+going\s+on(?:\s+here|\s+in\s+(?:the\s+)?(?:image|picture|photo))?\b",
            r"\bwhat\s+is\s+happening(?:\s+in\s+(?:the\s+)?(?:image|picture|photo))?\b",
            r"\bwhat\s+activity\s+is\s+shown\b",
            r"\bdescribe\s+the\s+activity\b",
        )
    )


def classify_question(question: str) -> str:
    normalized = normalize_question_text(question)
    q = f" {normalized} "
    if not q.strip():
        return "other"
    if is_opinion_or_evaluation_question(normalized):
        return "opinion_or_evaluation"
    if re.search(r"\b(read|text|say|says|written|words?|letters?|ocr)\b", q):
        return "text_reading"
    if re.search(r"\b(who is|who's|name|identify this person|identity)\b", q):
        return "person_identity"
    if re.search(r"\b(brand|make|model|ikea|origin|from what company)\b", q):
        return "brand_or_origin"
    if re.search(r"\b(happy|sad|angry|upset|emotion|feeling|intent|thinking|why)\b", q):
        return "emotion_or_intent"
    if re.search(r"\b(light|illumination|daylight|sunlight|glow|lit|lighting)\b", q) and re.search(
        r"\b(source|coming from|coming in|entering|through|where|cause|causing)\b",
        q,
    ):
        return "source_or_cause"
    asks_color = bool(re.search(r"\b(colou?r|what shade)\b", q))
    mentions_clothing = bool(re.search(
        r"\b(clothes|clothing|coat|dress|jacket|outerwear|pants|shirt|shorts|skirt|suit|sweater|trousers|wearing)\b",
        q,
    ))
    if asks_color and mentions_clothing:
        return "color_attribute"
    if mentions_clothing and re.search(r"\b(what|describe|wearing|clothes|clothing)\b", q):
        return "clothing_attribute"
    if asks_color:
        return "color_attribute"
    if re.search(r"\b(holding|carrying|touching|contact)\b", q):
        return "holding_or_contact"
    if re.search(r"\b(using|use|object .*using|what object|tool|instrument)\b", q):
        return "object_use"
    if question_mentions_fighting(question):
        return "action"
    if is_action_intent_question(question):
        return "action"
    if re.search(r"\b(?:doing|action|activity)\b", q):
        return "action"
    if re.search(r"\b(how many|number of|count)\b", q):
        return "object_count"
    if re.search(r"\b(is there|are there|do you see|can you see|visible|present)\b", q):
        return "object_presence"
    if re.search(r"\b(where|left|right|above|below|behind|front|near|next to|beside|on the desk|under)\b", q):
        return "spatial_relation"
    if re.search(r"\b(attribute|kind of|type of)\b", q):
        return "object_attribute"
    if re.search(r"\b(compare|similar|different|larger|smaller|better)\b", q):
        return "comparison"
    if re.search(r"\b(describe|what is in|what's in|tell me about)\b", q):
        return "scene_description"
    return "other"


def is_opinion_or_evaluation_question(normalized_question: str) -> bool:
    q = str(normalized_question or "")
    return any(
        re.search(pattern, q)
        for pattern in (
            r"\b(?:what is your opinion|what do you think (?:of|about)|what is your take|what is your view|your opinion|your take|give (?:me )?your opinion)\b",
            r"\b(?:judge|evaluate|evaluation|critique|rate|review)\b",
            r"\bis (?:this|that|the)\b.{0,48}\b(?:good|bad|effective|useful|scary|manipulative|persuasive|ethical|appealing|clear|successful)\b",
            r"\bdo you think\b.{0,48}\b(?:good|bad|effective|useful|scary|manipulative|persuasive|ethical|appealing|clear|successful)\b",
            r"\b(?:good|bad|effective|useful|scary|manipulative|persuasive|ethical|appealing|clear|successful)\s+(?:advertising|advertisement|ad|design|packaging|poster|label|warning|ui|interface)\b",
        )
    )


def extract_question_targets(question: str) -> list[str]:
    q = str(question or "").lower()
    question_type = classify_question(q)
    targets: list[str] = []
    if question_type in ACTION_QUESTION_TYPES:
        targets.append("person")
        targets.extend(sorted(actor_terms_for_question(question)))
        for term in ACTION_RELEVANT_ENTITIES:
            if re.search(rf"\b{re.escape(term)}s?\b", q):
                targets.append(term)
        if question_mentions_fighting(question):
            targets.extend(["fight", "argue"])
    if question_type == "source_or_cause" and re.search(r"\b(light|lighting|illumination|daylight|sunlight)\b", q):
        targets.extend(["light", "illumination"])
        if "daylight" in q or "sunlight" in q:
            targets.append("daylight")
    if "holding" in q:
        targets.extend(["person", "holding"])
    if question_type in ATTRIBUTE_QUESTION_TYPES:
        targets.extend(term for term in CLOTHING_TERMS if re.search(rf"\b{re.escape(term)}\b", q))
        if "wearing" in q or "clothes" in q or "clothing" in q:
            targets.extend(["person", "clothing"])
    if "desk" in q:
        targets.append("desk")
    if "glasses" in q:
        targets.extend(["glasses", "person"])
    pool_match = re.search(r"\b(swimming pool|pool)\b", q)
    if pool_match:
        targets.append("swimming pool")
    for phrase in re.findall(r"\b[a-z][a-z0-9-]*(?:\s+[a-z][a-z0-9-]*)?\b", q):
        words = [word for word in phrase.split() if word not in STOPWORDS]
        if words:
            candidate = canonicalize_label(" ".join(words))
            if candidate and len(candidate) <= 32:
                targets.append(candidate)
    return dedupe_strings(targets)[:8]


def deduplicate_objects(objects: list[dict[str, Any]]) -> list[dict[str, Any]]:
    deduped: list[dict[str, Any]] = []
    for raw in objects:
        label = str(raw.get("label") or raw.get("caption") or raw.get("phrase") or "").strip()
        canonical = canonicalize_label(label)
        if not canonical:
            continue
        box = normalized_box(raw.get("box"))
        source = str(raw.get("source") or "")
        match = None
        for item in deduped:
            if item["canonical_label"] != canonical:
                continue
            if box and item.get("box") and box_iou(box, item["box"]) >= 0.55:
                match = item
                break
            if not box and not item.get("box"):
                match = item
                break
        if match is None:
            item = {
                "label": canonical,
                "canonical_label": canonical,
                "raw_labels": [label],
                "count": 1,
                "box": box,
                "boxes": [box] if box else [],
                "source_tasks": [source] if source else [],
                "confidence": raw.get("confidence") if isinstance(raw.get("confidence"), (int, float)) else None,
            }
            deduped.append(item)
            continue
        if label and label not in match["raw_labels"]:
            match["raw_labels"].append(label)
        if source and source not in match["source_tasks"]:
            match["source_tasks"].append(source)
        if box:
            match.setdefault("boxes", []).append(box)
            match["box"] = merge_boxes([candidate for candidate in match["boxes"] if candidate])
        match["count"] = max(int(match.get("count") or 1), len(match.get("boxes") or []) or 1)
    return deduped


def build_visual_snippets(visual_evidence: dict[str, Any], objects: list[dict[str, Any]]) -> list[dict[str, Any]]:
    snippets: list[dict[str, Any]] = []
    seen: set[tuple[str, str]] = set()
    page_or_image_id = str(
        visual_evidence.get("artifact_id")
        or visual_evidence.get("page_or_image_id")
        or visual_evidence.get("image_id")
        or ""
    )

    def append(kind: str, text: str, *, source_task: str = "", extra_entities: list[str] | None = None) -> None:
        clean = re.sub(r"\s+", " ", str(text or "").strip())
        if not clean or kind not in VISUAL_SNIPPET_KINDS:
            return
        key = (kind, clean.lower())
        if key in seen:
            return
        seen.add(key)
        entities = snippet_entities(clean, extra_entities or [])
        snippets.append(
            {
                "id": f"V{len(snippets) + 1}",
                "kind": kind,
                "text": clean,
                "entities": entities,
                "actions": sorted(action_verbs(clean)),
                "source_task": source_task,
                "page_or_image_id": page_or_image_id,
                "confidence": None,
                "support": "direct" if kind not in {"derived_observation", "supported_inference"} else "derived",
                "primary_actor": primary_actor_for_text(clean),
            }
        )

    summary = str(visual_evidence.get("summary") or "")
    for sentence in split_sentences(summary):
        append("caption", sentence, source_task="more_detailed_caption")
        relation = relation_snippet_text(sentence)
        if relation:
            append("relation", relation, source_task="derived_relation")

    for item in visual_evidence.get("regions") or []:
        if not isinstance(item, dict):
            continue
        append(
            "dense_region_caption",
            str(item.get("caption") or ""),
            source_task=str(item.get("source") or "dense_region_caption"),
        )

    for item in objects:
        label = str(item.get("canonical_label") or item.get("label") or "").strip()
        append("object", label, source_task=", ".join(str(task) for task in item.get("source_tasks") or []) or "object_detection", extra_entities=[label])

    for item in visual_evidence.get("text") or []:
        if not isinstance(item, dict):
            continue
        append("ocr_text", str(item.get("text") or ""), source_task=str(item.get("source") or "ocr"))

    for item in visual_evidence.get("grounded_phrases") or []:
        if not isinstance(item, dict):
            continue
        append("derived_observation", str(item.get("phrase") or ""), source_task=str(item.get("source") or "phrase_grounding"))

    return snippets[:120]


def retrieve_visual_snippets(
    snippets: list[dict[str, Any]],
    question: str,
    question_type: str,
    targets: list[str],
    *,
    limit: int = 5,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    q = normalize_question_text(question)
    q_actors = actor_terms_for_question(question)
    q_actions = action_verbs(q)
    q_entities = question_entity_terms(question, targets)
    scored: list[tuple[int, int, dict[str, Any]]] = []

    for index, snippet in enumerate(snippets):
        score, signals = score_visual_snippet(snippet, q, q_actors, q_actions, q_entities, question_type)
        if score <= 0:
            continue
        item = dict(snippet)
        item["score"] = score
        item["matched_signals"] = signals
        scored.append((score, -index, item))

    if not scored and snippets:
        for index, snippet in enumerate(snippets):
            if snippet.get("kind") in {"caption", "dense_region_caption"}:
                item = dict(snippet)
                item["score"] = 1
                item["matched_signals"] = ["fallback_caption"]
                scored.append((1, -index, item))
                if len(scored) >= limit:
                    break

    retrieved = [item for _score, _index, item in sorted(scored, key=lambda row: (row[0], row[1]), reverse=True)[:limit]]
    metadata = {
        "schema": "odysseus.visual_evidence_retrieval.v1",
        "retriever_version": VISUAL_EVIDENCE_RETRIEVER_VERSION,
        "question_type": question_type,
        "question_text_hash": hashlib.sha256(q.encode("utf-8")).hexdigest(),
        "question_actor_targets": sorted(q_actors),
        "question_actions": sorted(q_actions),
        "question_entities": sorted(q_entities),
        "snippet_count": len(snippets),
        "retrieved_count": len(retrieved),
        "retrieval_recomputed": True,
    }
    return retrieved, metadata


def score_visual_snippet(
    snippet: dict[str, Any],
    normalized_question: str,
    q_actors: set[str],
    q_actions: set[str],
    q_entities: set[str],
    question_type: str,
) -> tuple[int, list[str]]:
    text = str(snippet.get("text") or "")
    lower = text.lower()
    kind = str(snippet.get("kind") or "")
    entities = {str(item).lower() for item in snippet.get("entities") or []}
    actions = {str(item).lower() for item in snippet.get("actions") or []}
    actors = actor_terms_for_text(text).union(entities.intersection({"man", "woman", "person", "people"}))
    primary_actor = str(snippet.get("primary_actor") or "")
    priority = source_priority(kind)
    score = 0
    signals: list[str] = []

    overlap = q_entities.intersection(entities)
    if overlap:
        score += min(12, len(overlap) * 3)
        signals.append("entity:" + ",".join(sorted(overlap)[:4]))
    for token in set(normalized_question.split()):
        if len(token) >= 4 and token not in STOPWORDS and re.search(rf"\b{re.escape(token)}s?\b", lower):
            score += 1

    if q_actors:
        actor_score, actor_signals = actor_match_score(q_actors, actors, primary_actor)
        score += actor_score
        signals.extend(actor_signals)

    if question_type in ACTION_QUESTION_TYPES:
        if kind == "object":
            score -= 3
        if actions:
            score += 8
            signals.append("has_action")
        if q_actions:
            compatible = expanded_actions(q_actions).intersection(expanded_actions(actions))
            if compatible:
                score += 20
                signals.append("action:" + ",".join(sorted(compatible)))
            elif q_actions.intersection({"fight", "argue"}):
                score += 2
                signals.append("actor_context_for_absence_question")
        elif actions and q_actors and actor_match_score(q_actors, actors, primary_actor)[0] > 0:
            score += 12
            signals.append("actor_action")
        action_objects = entities.intersection(ACTION_RELEVANT_ENTITIES)
        if action_objects:
            score += min(10, len(action_objects) * 2)
            signals.append("action_object:" + ",".join(sorted(action_objects)[:4]))
    elif question_type == "source_or_cause":
        light = entities.intersection(LIGHT_SOURCE_ENTITIES)
        if light:
            score += 16
            signals.append("light_source:" + ",".join(sorted(light)))
    elif question_type in {"object_presence", "object_identity", "object_attribute", "spatial_relation"}:
        if q_entities.intersection(entities):
            score += 10
    elif question_type in {"color_attribute", "clothing_attribute"}:
        clothing_matches = {term for term in CLOTHING_TERMS if re.search(rf"\b{re.escape(term)}\b", lower)}
        color_matches = {term for term in COLOR_TERMS if re.search(rf"\b{re.escape(term)}\b", lower)}
        if clothing_matches:
            score += 20
            signals.append("clothing:" + ",".join(sorted(clothing_matches)))
        if color_matches:
            score += 18
            signals.append("color:" + ",".join(sorted(color_matches)))
        if "wear" in actions or re.search(r"\bwear(?:s|ing)?\b", lower):
            score += 14
            signals.append("wearing")
        if question_type == "color_attribute" and not color_matches:
            score -= 5
        if re.search(r"\bhold(?:s|ing)?\b", lower) and not clothing_matches:
            score -= 12

    if score > 0:
        score += priority
        signals.append(f"source:{kind}")
    return score, dedupe_strings(signals)


def actor_match_score(q_actors: set[str], snippet_actors: set[str], primary_actor: str) -> tuple[int, list[str]]:
    signals: list[str] = []
    if not q_actors:
        return 0, signals
    if "people" in q_actors:
        if {"man", "woman"}.issubset(snippet_actors) or "people" in snippet_actors:
            return 18, ["actor:people"]
        if snippet_actors:
            return 6, ["actor:any_person"]
    score = 0
    for actor in sorted(q_actors):
        if actor == "person":
            if snippet_actors:
                score += 8
                signals.append("actor:person")
            continue
        if primary_actor == actor:
            score += 24
            signals.append(f"primary_actor:{actor}")
        elif actor in snippet_actors:
            score += 6
            signals.append(f"mentioned_actor:{actor}")
        opposite = "woman" if actor == "man" else "man" if actor == "woman" else ""
        if opposite and primary_actor == opposite:
            score -= 18
            signals.append(f"primary_actor_mismatch:{opposite}")
    if {"man", "woman"}.issubset(q_actors) and {"man", "woman"}.issubset(snippet_actors):
        score += 10
        signals.append("actor_pair")
    return score, signals


def source_priority(kind: str) -> int:
    return {
        "caption": 8,
        "dense_region_caption": 8,
        "relation": 6,
        "derived_observation": 5,
        "supported_inference": 4,
        "ocr_text": 3,
        "object": 1,
    }.get(kind, 0)


def question_entity_terms(question: str, targets: list[str]) -> set[str]:
    terms = {term for term in actor_terms_for_question(question)}
    for target in targets:
        clean = str(target or "").strip().lower()
        if clean:
            terms.add(clean)
            terms.add(canonicalize_label(clean))
    q = normalize_question_text(question)
    for phrase in ("cutting board", "light bulb", "swimming pool", "picture frame"):
        if phrase in q:
            terms.add(phrase)
    for token in q.split():
        if token in STOPWORDS or token in ACTION_VERB_ALIASES or token in ACTION_VERB_ALIASES.values():
            continue
        if len(token) >= 3:
            terms.add(canonicalize_label(token))
    return {term for term in terms if term}


def snippet_entities(text: str, extra_entities: list[str]) -> list[str]:
    lower = str(text or "").lower()
    entities: set[str] = set()
    entities.update(actor_terms_for_text(lower))
    for phrase in ("cutting board", "light bulb", "swimming pool", "picture frame"):
        if re.search(rf"\b{re.escape(phrase)}s?\b", lower):
            entities.add(phrase)
    for item in extra_entities:
        clean = canonicalize_label(str(item or ""))
        if clean:
            entities.add(clean)
    for token in re.findall(r"\b[a-z][a-z-]{2,}\b", lower):
        if token in STOPWORDS or token in ACTION_VERB_ALIASES or token in ACTION_VERB_ALIASES.values():
            continue
        canonical = canonicalize_label(token)
        if canonical:
            entities.add(canonical)
    return sorted(entities)


def actor_terms_for_text(text: str) -> set[str]:
    lower = str(text or "").lower()
    actors: set[str] = set()
    if re.search(r"\b(man|men|boy|he|him)\b", lower):
        actors.add("man")
        actors.add("person")
    if re.search(r"\b(woman|women|girl|she|her)\b", lower):
        actors.add("woman")
        actors.add("person")
    if re.search(r"\b(person|people|someone|they|them)\b", lower):
        actors.add("person")
    if re.search(r"\b(people|they|them)\b", lower):
        actors.add("people")
    return actors


def primary_actor_for_text(text: str) -> str:
    lower = str(text or "").strip().lower()
    if re.match(r"^(?:a|the)?\s*(?:man|boy|he)\b", lower):
        return "man"
    if re.match(r"^(?:a|the)?\s*(?:woman|girl|she)\b", lower):
        return "woman"
    if re.match(r"^(?:a|the)?\s*(?:person|someone)\b", lower):
        return "person"
    if re.match(r"^(?:the\s+)?(?:people|they)\b", lower):
        return "people"
    if re.match(r"^(?:a|the)?\s*(?:man|person)\s+and\s+(?:a\s+|the\s+)?woman\b", lower):
        return "people"
    return ""


def relation_snippet_text(sentence: str) -> str:
    lower = str(sentence or "").lower()
    if re.search(r"\bwoman\b", lower) and re.search(r"\bman\b", lower) and re.search(r"\b(next to|near|beside|standing next to)\b", lower):
        return "woman next to man"
    return ""


def expanded_actions(actions: set[str]) -> set[str]:
    result = set(actions)
    if result.intersection({"cut", "prepare"}):
        result.update({"cut", "prepare", "cook"})
    if result.intersection({"fight", "argue"}):
        result.update({"fight", "argue"})
    return result


def curate_visual_evidence(visual_evidence: dict[str, Any], question: str) -> dict[str, Any]:
    question_type = classify_question(question)
    targets = extract_question_targets(question)
    raw_objects = visual_evidence.get("objects") if isinstance(visual_evidence.get("objects"), list) else []
    objects = deduplicate_objects([item for item in raw_objects if isinstance(item, dict)])
    summary = str(visual_evidence.get("summary") or "").strip()
    snippets = build_visual_snippets(visual_evidence, objects)
    retrieved_snippets, retrieval_metadata = retrieve_visual_snippets(snippets, question, question_type, targets)
    retrieval_metadata.update({
        "artifact_id": str(visual_evidence.get("artifact_id") or visual_evidence.get("page_or_image_id") or visual_evidence.get("image_id") or ""),
        "raw_evidence_version": str(visual_evidence.get("schema") or "unknown"),
        "cache_key": hashlib.sha256(
            "|".join([
                str(visual_evidence.get("artifact_id") or visual_evidence.get("page_or_image_id") or visual_evidence.get("image_id") or ""),
                str(visual_evidence.get("schema") or "unknown"),
                VISUAL_EVIDENCE_RETRIEVER_VERSION,
                retrieval_metadata["question_text_hash"],
                question_type,
            ]).encode("utf-8")
        ).hexdigest(),
    })
    relevant_labels = relevant_label_set(question_type, targets)
    relevant_objects = select_relevant_objects(objects, relevant_labels, question_type)
    observations = build_observations(summary, relevant_objects, question_type, retrieved_snippets=retrieved_snippets)
    allowed = build_allowed_inferences(observations, question_type)
    excluded = [
        item["canonical_label"]
        for item in objects
        if item["canonical_label"] not in {entity for obs in observations for entity in obs["entities"]}
    ]
    unsupported = unsupported_requests(question_type)
    warnings: list[str] = []
    if not observations and summary:
        observations.append({
            "id": "O1",
            "text": summary,
            "entities": [],
            "source_tasks": ["more_detailed_caption"],
            "support": "direct",
        })
    if not observations:
        warnings.append("No compact visual observations matched this question.")
    return {
        "schema": "odysseus.curated_visual_evidence.v1",
        "curator_version": VISUAL_EVIDENCE_CURATOR_VERSION,
        "retriever_version": VISUAL_EVIDENCE_RETRIEVER_VERSION,
        "question_type": question_type,
        "question_targets": targets,
        "visual_snippets": snippets,
        "retrieved_visual_snippets": retrieved_snippets,
        "retrieval_metadata": retrieval_metadata,
        "direct_observations": observations[:8],
        "allowed_inferences": allowed[:5],
        "unsupported_requests": unsupported,
        "excluded_irrelevant_entities": dedupe_strings(excluded)[:20],
        "deduplicated_objects": objects[:40],
        "warnings": warnings,
        "raw_evidence_available": bool(visual_evidence),
    }


def visual_evidence_from_observations(observations: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(observations, dict):
        return {}
    summary = str(observations.get("summary") or "").strip()
    objects: list[dict[str, Any]] = []
    for key in ("visible_objects", "interface_elements"):
        value = observations.get(key)
        if not isinstance(value, list):
            continue
        for item in value:
            label = str(item or "").strip()
            if label:
                objects.append({"label": label, "source": key})
    regions: list[dict[str, Any]] = []
    for key in ("spatial_relations", "uncertain_observations", "not_visible_or_not_determinable"):
        value = observations.get(key)
        if not isinstance(value, list):
            continue
        for item in value:
            caption = str(item or "").strip()
            if caption:
                regions.append({"caption": caption, "source": key})
    text_items = [
        {"text": str(item).strip(), "source": "model_visible_text"}
        for item in observations.get("model_visible_text") or []
        if str(item).strip()
    ] if isinstance(observations.get("model_visible_text"), list) else []
    if not summary and not objects and not regions and not text_items:
        return {}
    return {
        "schema": "odysseus.visual_evidence_from_observations.v1",
        "summary": summary,
        "objects": objects,
        "regions": regions,
        "text": text_items,
        "grounded_phrases": [],
        "uncertain": observations.get("uncertain_observations") if isinstance(observations.get("uncertain_observations"), list) else [],
        "not_determinable": observations.get("not_visible_or_not_determinable") if isinstance(observations.get("not_visible_or_not_determinable"), list) else [],
    }


def compact_synthesis_packet(question: str, curated: dict[str, Any], *, ocr_text: str = "") -> str:
    lines = ["USER QUESTION", str(question or "").strip() or "[empty]"]
    metadata = curated.get("retrieval_metadata") if isinstance(curated.get("retrieval_metadata"), dict) else {}
    lines.extend(
        [
            "",
            "VISUAL RETRIEVAL METADATA",
            f"Question type: {curated.get('question_type') or metadata.get('question_type') or 'unknown'}",
            f"Retrieved snippets: {metadata.get('retrieved_count', 0)} of {metadata.get('snippet_count', 0)}",
            f"Retriever version: {curated.get('retriever_version') or metadata.get('retriever_version') or 'unknown'}",
        ]
    )
    lines.extend(["", "RETRIEVED VISUAL EVIDENCE"])
    snippets = curated.get("retrieved_visual_snippets") if isinstance(curated.get("retrieved_visual_snippets"), list) else []
    if snippets:
        for snippet in snippets:
            if isinstance(snippet, dict):
                lines.append(f"[{snippet.get('id')}] {snippet.get('text')}")
    else:
        lines.append("[none]")
    lines.extend(["", "DIRECTLY OBSERVED"])
    observations = curated.get("direct_observations") if isinstance(curated.get("direct_observations"), list) else []
    if observations:
        for obs in observations:
            if isinstance(obs, dict):
                lines.append(f"[{obs.get('id')}] {obs.get('text')}")
    else:
        lines.append("[none]")
    lines.extend(["", "SUPPORTED INFERENCE"])
    inferences = curated.get("allowed_inferences") if isinstance(curated.get("allowed_inferences"), list) else []
    if inferences:
        for inference in inferences:
            if isinstance(inference, dict):
                support = ", ".join(str(item) for item in inference.get("supported_by") or [])
                suffix = f" Supported by {support}." if support else ""
                lines.append(f"[{inference.get('id')}] {inference.get('text')}{suffix}")
    else:
        lines.append("[none]")
    unsupported = [str(item) for item in curated.get("unsupported_requests") or [] if str(item).strip()]
    excluded = [str(item) for item in curated.get("excluded_irrelevant_entities") or [] if str(item).strip()]
    lines.extend(["", "NOT SUPPORTED"])
    if unsupported:
        lines.extend(unsupported)
    if excluded:
        lines.append(f"Do not mention these excluded entities unless the user explicitly asks about them: {', '.join(excluded[:12])}.")
    if curated.get("question_type") == "source_or_cause":
        lines.append("Do not claim that wall pictures, furniture, books, hats, plants, or other unrelated objects produce light.")
    if curated.get("question_type") in ACTION_QUESTION_TYPES:
        lines.append("For action questions, preserve direct action observations exactly and do not replace them with generic visibility.")
        lines.append("If a direct observation says a person is sitting, reading, holding, using, or riding something, do not answer with a contradictory action.")
        lines.append("Do not answer with evidence about the woman when the question asks what the man is doing, or vice versa.")
    if curated.get("question_type") in {"color_attribute", "clothing_attribute"}:
        lines.append("Answer the current clothing or color question; do not repeat an earlier holding or action answer.")
        lines.append("Do not infer a garment or object color unless a retrieved snippet directly states it.")
    if ocr_text.strip():
        lines.extend(["", "OCR TEXT", ocr_text.strip()])
    answer_rules = [
        "",
        "ANSWER RULES",
        "- Answer directly.",
        "- Prefer direct caption or dense-region evidence over object-only evidence.",
        "- Do not add causes, brands, places, identities, motives, or emotions.",
        "- Use may, appears, or likely for inference.",
        "- Do not start with boilerplate about structured visual evidence.",
    ]
    if curated.get("question_type") == "opinion_or_evaluation":
        answer_rules.extend([
            "- Base factual claims on the retrieved visual evidence, direct observations, and supported inference above.",
            "- Use four labeled parts in order: Visible facts; Reasonable visual interpretation; Opinion; Limits.",
            "- Give a clearly labeled opinion based on the visible facts; an opinion is a judgment, not a claim that opinion text is visible.",
            "- Do not refuse merely because the image contains no explicit opinion text.",
            "- Do not claim hidden intent, manufacturer or designer intent, exact jurisdiction, measured effectiveness, statistics, or real-world outcomes unless provided.",
        ])
    else:
        answer_rules.append("- Use only the retrieved visual evidence, direct observations, and supported inference above.")
    lines.extend(answer_rules)
    return "\n".join(lines)


def guard_visual_answer(answer: str, curated: dict[str, Any]) -> dict[str, Any]:
    text = str(answer or "")
    lower = text.lower()
    supported_entities = {
        canonicalize_label(entity)
        for obs in curated.get("direct_observations") or []
        if isinstance(obs, dict)
        for entity in obs.get("entities") or []
    }
    supported_entities.update({
        canonicalize_label(entity)
        for inference in curated.get("allowed_inferences") or []
        if isinstance(inference, dict)
        for entity in re.findall(r"\b[a-z][a-z-]+\b", str(inference.get("text") or "").lower())
    })
    for excluded in curated.get("excluded_irrelevant_entities") or []:
        label = canonicalize_label(str(excluded))
        label_tokens = {
            canonicalize_label(token)
            for token in re.findall(r"\b[a-z][a-z-]+\b", label)
            if canonicalize_label(token)
        }
        label_is_supported = label in supported_entities or bool(
            label_tokens and label_tokens.issubset(supported_entities)
        )
        if label and not label_is_supported and re.search(rf"\b{re.escape(label)}s?\b", lower):
            return guard_failure("excluded_entity", f"Answer mentioned excluded entity: {label}")
    for brand in ("ikea", "apple", "nike", "samsung", "google"):
        if brand in lower and brand not in supported_entities:
            return guard_failure("unsupported_brand", f"Answer introduced unsupported brand: {brand}")
    for location in ("london", "paris", "new york", "india", "america"):
        if location in lower and location not in supported_entities:
            return guard_failure("unsupported_location", f"Answer introduced unsupported location: {location}")
    if re.search(r"\b(Sir|Dr|Mr|Mrs|Ms)\.?\s+[A-Z][a-z]+", text):
        return guard_failure("unsupported_person_name", "Answer introduced an unsupported person name.")
    if curated.get("question_type") == "source_or_cause":
        targets = {canonicalize_label(str(item)) for item in curated.get("question_targets") or []}
        inference_text = " ".join(
            str(inference.get("text") or "")
            for inference in curated.get("allowed_inferences") or []
            if isinstance(inference, dict)
        ).lower()
        if "daylight" in targets and "window" in inference_text and not re.search(r"\b(window|daylight|sunlight)\b", lower):
            return guard_failure("missing_question_target", "Answer did not address the daylight/window evidence.")
        allowed_sources = supported_entities.intersection(LIGHT_SOURCE_ENTITIES)
        causal_terms = re.findall(r"\b([a-z][a-z-]+)\b(?=[^.]{0,32}\b(?:light|illuminat|source|glow|daylight))", lower)
        ignored_causal_terms = {"a", "an", "and", "the", "main", "secondary", "ambient", "warm", "visible", "likely", "possible"}
        for term in causal_terms:
            canonical = canonicalize_label(term)
            if canonical and canonical not in allowed_sources and canonical not in ignored_causal_terms:
                return guard_failure("unsupported_causal_source", f"Answer introduced unsupported light source: {canonical}")
        light_source_answer = deterministic_light_source_answer(curated)
        if light_source_answer:
            required_role_terms = []
            if "main" in light_source_answer.lower():
                required_role_terms.append("main")
            if "secondary" in light_source_answer.lower():
                required_role_terms.append("secondary")
            missing_role_terms = [term for term in required_role_terms if term not in lower]
            if missing_role_terms:
                return guard_failure(
                    "missing_light_source_role",
                    "Answer did not preserve supported light-source role: "
                    + ", ".join(missing_role_terms),
                )
    if curated.get("question_type") in ACTION_QUESTION_TYPES:
        evidence_text = visual_evidence_text(curated)
        supported_actions = action_verbs(evidence_text)
        answer_actions = guard_action_verbs(text)
        q_actors = question_actor_targets_from_curated(curated)
        primary_answer_actor = primary_actor_for_text(text)
        unsupported = sorted(
            verb
            for verb in answer_actions
            if verb in UNSUPPORTED_ACTION_GUARD_VERBS and verb not in supported_actions
        )
        if unsupported:
            return guard_failure(
                "unsupported_action",
                f"Answer introduced unsupported action: {', '.join(unsupported)}",
            )
        if q_actors in ({"man"}, {"woman"}) and primary_answer_actor and primary_answer_actor not in q_actors:
            return guard_failure(
                "actor_mismatch",
                f"Answer led with {primary_answer_actor} evidence for a question about {', '.join(sorted(q_actors))}.",
            )
        top_snippet = top_retrieved_action_snippet(curated)
        top_actions = {str(item) for item in (top_snippet or {}).get("actions") or []}
        if top_actions and answer_actions and not expanded_actions(top_actions).intersection(expanded_actions(answer_actions)):
            return guard_failure(
                "contradicts_top_visual_snippet",
                f"Answer actions {', '.join(sorted(answer_actions))} do not match top visual evidence {', '.join(sorted(top_actions))}.",
            )
    if curated.get("question_type") in {"color_attribute", "clothing_attribute"}:
        if re.search(r"\bhold(?:s|ing)?\b", lower) and not re.search(r"\b(?:" + "|".join(sorted(COLOR_TERMS)) + r")\b", lower):
            return guard_failure("question_type_mismatch", "answer_repeated_previous_question")
        if curated.get("question_type") == "color_attribute" and not re.search(
            r"\b(?:" + "|".join(sorted(COLOR_TERMS)) + r")\b|does not clearly state the color",
            lower,
        ):
            return guard_failure("attribute_answer_missing_attribute", "attribute_answer_missing_attribute")
    return {
        "ok": True,
        "grounding_guard_triggered": False,
        "grounding_guard_reason": "",
        "regeneration_attempted": False,
        "safe_fallback_used": False,
    }


def safe_fallback_answer(question: str, curated: dict[str, Any]) -> str:
    question_type = str(curated.get("question_type") or classify_question(question))
    if question_type == "person_identity":
        clean_question = str(question or "").lower()
        if re.search(r"\b(?:she|her|woman|girl)\b", clean_question):
            return "I can't identify who she is from the image."
        if re.search(r"\b(?:he|him|man|boy)\b", clean_question):
            return "I can't identify who he is from the image."
        return "I can't identify who this person is from the image."
    if question_type in {"brand_or_origin", "emotion_or_intent"}:
        return "The available image evidence does not support that claim."
    deterministic = deterministic_visual_answer(question, curated)
    if deterministic:
        return deterministic
    inferences = [
        str(item.get("text") or "").strip()
        for item in curated.get("allowed_inferences") or []
        if isinstance(item, dict) and str(item.get("text") or "").strip()
    ]
    if inferences:
        return " ".join(inferences)
    observations = [
        str(item.get("text") or "").strip()
        for item in curated.get("direct_observations") or []
        if isinstance(item, dict) and str(item.get("text") or "").strip()
    ]
    if observations:
        prefix = "From the available image evidence, "
        return prefix + " ".join(observations[:3])
    return "The available image evidence is not specific enough to answer that."


def deterministic_visual_answer(question: str, curated: dict[str, Any]) -> str:
    question_type = str(curated.get("question_type") or classify_question(question))
    if question_type == "source_or_cause" and re.search(
        r"\b(light|lighting|illumination|daylight|sunlight)\b",
        str(question or ""),
        re.IGNORECASE,
    ):
        light_answer = deterministic_light_source_answer(curated)
        if light_answer:
            return light_answer
    if question_type in {"color_attribute", "clothing_attribute"}:
        return deterministic_attribute_answer(question, curated)
    if question_type not in ACTION_QUESTION_TYPES:
        return ""
    if question_mentions_fighting(question):
        return deterministic_fighting_answer(curated)
    observation = best_action_observation(curated, question)
    if not observation:
        return ""
    text = str(observation.get("text") or "").strip()
    if not text:
        return ""
    lower = text.lower()
    if re.search(r"\b(?:man|person|he)\b", lower) and "read" in action_verbs(text) and "book" in lower:
        if "sit" in action_verbs(text) or "chair" in lower or "seated" in lower:
            return "He appears to be sitting in a chair and reading a book."
        return "He appears to be reading a book."
    if re.search(r"\b(?:man|person|he)\b", lower) and "cut" in action_verbs(text):
        if "vegetable" in lower and "cutting board" in lower:
            return "He appears to be cutting vegetables on a cutting board."
        if "vegetable" in lower:
            return "He appears to be cutting vegetables."
        return "He appears to be cutting something."
    if re.search(r"\b(?:woman|she)\b", lower) and "stand" in action_verbs(text) and re.search(r"\b(?:near|next to|beside)\b", lower):
        if "man" in lower:
            return "She appears to be standing near the man."
        return "She appears to be standing nearby."
    if re.search(r"\b(?:woman|she)\b", lower) and "hold" in action_verbs(text):
        held = first_action_object(lower, "holding") or first_action_object(lower, "holding a")
        return f"She appears to be holding {held}." if held else "She appears to be holding something."
    if "person" in lower and "use" in action_verbs(text):
        used = first_action_object(lower, "using")
        return f"The person appears to be using {used}." if used else "The person appears to be using an object."
    cleaned = text.rstrip(".")
    cleaned = re.sub(r"^a\s+man\s+is\s+", "He appears to be ", cleaned, flags=re.IGNORECASE)
    cleaned = re.sub(r"^a\s+woman\s+is\s+", "She appears to be ", cleaned, flags=re.IGNORECASE)
    cleaned = re.sub(r"^a\s+person\s+is\s+", "The person appears to be ", cleaned, flags=re.IGNORECASE)
    if cleaned == text.rstrip("."):
        return f"The image evidence shows: {cleaned}."
    return cleaned[:1].upper() + cleaned[1:] + "."


def deterministic_attribute_answer(question: str, curated: dict[str, Any]) -> str:
    q = normalize_question_text(question)
    snippets = curated.get("retrieved_visual_snippets") if isinstance(curated.get("retrieved_visual_snippets"), list) else []
    evidence = " ".join(str(item.get("text") or "") for item in snippets if isinstance(item, dict))
    lower = evidence.lower()

    def garment_color(terms: tuple[str, ...]) -> str:
        garment = "(?:" + "|".join(re.escape(term) for term in terms) + ")"
        colors = "(?:" + "|".join(sorted(COLOR_TERMS)) + ")"
        patterns = (
            rf"\b{colors}\s+{garment}\b",
            rf"\b{garment}\s+(?:is|appears|looks)\s+{colors}\b",
        )
        for pattern in patterns:
            match = re.search(pattern, lower)
            if match:
                return next((color for color in COLOR_TERMS if re.search(rf"\b{color}\b", match.group(0))), "")
        return ""

    if re.search(r"\b(pants|trousers)\b", q):
        color = garment_color(("pants", "trousers"))
        return f"His pants appear to be {color}." if color else "The image evidence does not clearly state the color of his pants."
    if re.search(r"\b(outerwear|jacket|coat|outer layer)\b", q):
        color = garment_color(("outerwear", "jacket", "coat", "outer layer"))
        if color:
            return f"His outerwear appears to be {color}."
        return "The image evidence does not clearly state the color of his outerwear."
    if re.search(r"\bthing\s+(?:he|she|they)\s+(?:is|are)\s+holding\b", q):
        return "The image evidence does not clearly state the color of the objects he is holding."
    if re.search(r"\b(what is he wearing|describe his clothing|what clothes is he wearing|what is she wearing|what are they wearing)\b", q):
        match = re.search(r"\bwearing\s+([^.!?]+)", evidence, re.IGNORECASE)
        if match:
            clothing = match.group(1).strip().rstrip(".")
            return f"He appears to be wearing {clothing}."
    return ""


def deterministic_fighting_answer(curated: dict[str, Any]) -> str:
    evidence_text = visual_evidence_text(curated)
    verbs = action_verbs(evidence_text)
    if verbs.intersection({"fight", "argue"}):
        return ""
    lower = evidence_text.lower()
    has_kitchen = "kitchen" in lower
    has_man_cutting = "man" in lower and "cut" in verbs and ("vegetable" in lower or "cutting board" in lower)
    has_woman_nearby = "woman" in lower and re.search(r"\b(standing|near|next to|beside)\b", lower)
    if has_man_cutting and has_woman_nearby:
        place = " in a kitchen" if has_kitchen else ""
        return (
            "The available visual evidence does not show them fighting. "
            f"It shows them{place}, with the man cutting vegetables and the woman standing nearby."
        )
    return "The available visual evidence does not show them fighting."


def deterministic_light_source_answer(curated: dict[str, Any]) -> str:
    inferences = [
        str(item.get("text") or "").strip()
        for item in curated.get("allowed_inferences") or []
        if isinstance(item, dict) and str(item.get("text") or "").strip()
    ]
    lower = " ".join(inferences).lower()
    has_lamp = "lamp" in lower and "main" in lower and "light source" in lower
    has_window = "window" in lower and "secondary" in lower and "daylight" in lower
    if has_lamp and has_window:
        return "The lamp appears to be the main light source. The window may provide secondary ambient daylight."
    if has_lamp:
        return "The lamp appears to be the main light source."
    if has_window:
        return "The window may provide secondary ambient light."
    return ""


def warning_affects_status(warning: str, question: str) -> bool:
    clean = str(warning or "").strip()
    if clean == OCR_NO_TEXT_WARNING and classify_question(question) != "text_reading":
        return False
    return bool(clean)


def status_from_warnings(warnings: list[str], question: str, *, default: str = "completed") -> str:
    return "completed_with_warnings" if any(warning_affects_status(warning, question) for warning in warnings) else default


def relevant_label_set(question_type: str, targets: list[str]) -> set[str]:
    relevant = {canonicalize_label(item) for item in targets if item}
    if question_type == "source_or_cause" and {"light", "illumination", "daylight", "sunlight"}.intersection(relevant):
        relevant.update(LIGHT_SOURCE_ENTITIES)
    if question_type == "holding_or_contact":
        relevant.update({"person", "hand", "book", "flag", "phone", "tool"})
    if question_type in ACTION_QUESTION_TYPES:
        relevant.update({"person", *ACTION_RELEVANT_ENTITIES})
    if question_type in {"color_attribute", "clothing_attribute"}:
        relevant.update({"person", *CLOTHING_TERMS})
    return {item for item in relevant if item}


def select_relevant_objects(objects: list[dict[str, Any]], labels: set[str], question_type: str) -> list[dict[str, Any]]:
    if not labels and question_type in {"scene_description", "other"}:
        return objects[:10]
    selected = [item for item in objects if item["canonical_label"] in labels]
    if question_type == "source_or_cause":
        selected = [item for item in selected if item["canonical_label"] in LIGHT_SOURCE_ENTITIES]
    return selected[:10]


def build_observations(
    summary: str,
    objects: list[dict[str, Any]],
    question_type: str,
    *,
    retrieved_snippets: list[dict[str, Any]] | None = None,
) -> list[dict[str, Any]]:
    observations: list[dict[str, Any]] = []
    seen_texts: set[str] = set()
    selected_entities = {item["canonical_label"] for item in objects}
    if question_type == "source_or_cause":
        selected_entities.update(LIGHT_SOURCE_ENTITIES)
    if question_type in ACTION_QUESTION_TYPES:
        selected_entities.update({"person", *ACTION_RELEVANT_ENTITIES})
    if question_type in {"color_attribute", "clothing_attribute"}:
        selected_entities.update({"person", *CLOTHING_TERMS})
    for snippet in retrieved_snippets or []:
        if not isinstance(snippet, dict):
            continue
        kind = str(snippet.get("kind") or "")
        text = str(snippet.get("text") or "").strip()
        if not text or kind == "object":
            continue
        if question_type in ACTION_QUESTION_TYPES and not useful_action_sentence(text) and kind != "relation":
            continue
        if text.lower() in seen_texts:
            continue
        seen_texts.add(text.lower())
        observations.append({
            "id": f"O{len(observations) + 1}",
            "text": text,
            "entities": sorted({str(item) for item in snippet.get("entities") or [] if str(item)}),
            "source_tasks": [str(snippet.get("source_task") or kind)],
            "support": str(snippet.get("support") or "direct"),
            "source_snippet_id": str(snippet.get("id") or ""),
        })
    for sentence in split_sentences(summary):
        if sentence.lower() in seen_texts:
            continue
        if question_type in ACTION_QUESTION_TYPES and useful_action_sentence(sentence):
            sentence_entities = action_sentence_entities(sentence, selected_entities)
            observations.append({
                "id": f"O{len(observations) + 1}",
                "text": sentence,
                "entities": sorted(sentence_entities),
                "source_tasks": ["more_detailed_caption"],
                "support": "direct",
            })
            seen_texts.add(sentence.lower())
            continue
        sentence_entities = {label for label in selected_entities if re.search(rf"\b{re.escape(label)}s?\b", sentence.lower())}
        if question_type == "source_or_cause" and sentence_entities and not useful_light_context_sentence(sentence, sentence_entities):
            continue
        if sentence_entities:
            observations.append({
                "id": f"O{len(observations) + 1}",
                "text": sentence,
                "entities": sorted(sentence_entities),
                "source_tasks": ["more_detailed_caption"],
                "support": "direct",
            })
            seen_texts.add(sentence.lower())
    observed_entities = {entity for obs in observations for entity in obs["entities"]}
    for item in objects:
        label = item["canonical_label"]
        if label in observed_entities:
            continue
        text = object_observation_text(item, question_type, selected_entities)
        observations.append({
            "id": f"O{len(observations) + 1}",
            "text": text,
            "entities": [label],
            "source_tasks": item.get("source_tasks") or ["object_detection"],
            "support": "direct",
        })
    return observations


def build_allowed_inferences(observations: list[dict[str, Any]], question_type: str) -> list[dict[str, Any]]:
    if question_type in ACTION_QUESTION_TYPES:
        return build_action_inferences(observations)
    if question_type != "source_or_cause":
        return []
    result: list[dict[str, Any]] = []
    added_entities: set[str] = set()
    for obs in observations:
        entities = {canonicalize_label(str(item)) for item in obs.get("entities") or []}
        if "lamp" in entities and "lamp" not in added_entities:
            added_entities.add("lamp")
            result.append({
                "id": f"I{len(result) + 1}",
                "text": "The lamp is likely the main visible light source.",
                "supported_by": [obs["id"]],
                "certainty": "likely",
            })
        if "window" in entities and "window" not in added_entities:
            added_entities.add("window")
            result.append({
                "id": f"I{len(result) + 1}",
                "text": "The window may provide secondary ambient daylight.",
                "supported_by": [obs["id"]],
                "certainty": "possible",
            })
    return result


def build_action_inferences(observations: list[dict[str, Any]]) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    for obs in observations:
        text = str(obs.get("text") or "")
        lower = text.lower()
        verbs = action_verbs(text)
        if "read" in verbs and "book" in lower and ("sit" in verbs or "chair" in lower or "seated" in lower):
            result.append({
                "id": f"I{len(result) + 1}",
                "text": "He appears to be reading while seated.",
                "supported_by": [str(obs.get("id") or "")],
                "certainty": "appears",
            })
        elif "read" in verbs and "book" in lower:
            result.append({
                "id": f"I{len(result) + 1}",
                "text": "He appears to be reading a book.",
                "supported_by": [str(obs.get("id") or "")],
                "certainty": "appears",
            })
        elif "cut" in verbs and ("vegetable" in lower or "cutting board" in lower):
            result.append({
                "id": f"I{len(result) + 1}",
                "text": "He appears to be cutting vegetables on a cutting board.",
                "supported_by": [str(obs.get("id") or "")],
                "certainty": "appears",
            })
        elif "stand" in verbs and "woman" in lower and "man" in lower:
            result.append({
                "id": f"I{len(result) + 1}",
                "text": "She appears to be standing near the man.",
                "supported_by": [str(obs.get("id") or "")],
                "certainty": "appears",
            })
        elif "hold" in verbs:
            result.append({
                "id": f"I{len(result) + 1}",
                "text": "The person appears to be holding an object.",
                "supported_by": [str(obs.get("id") or "")],
                "certainty": "appears",
            })
        elif "use" in verbs:
            result.append({
                "id": f"I{len(result) + 1}",
                "text": "The person appears to be using an object.",
                "supported_by": [str(obs.get("id") or "")],
                "certainty": "appears",
            })
    return result[:3]


def unsupported_requests(question_type: str) -> list[str]:
    if question_type in {"person_identity", "brand_or_origin", "emotion_or_intent"}:
        return [UNSUPPORTED_IDENTITY_TEXT]
    return []


def useful_light_context_sentence(sentence: str, entities: set[str]) -> bool:
    lower = sentence.lower()
    if "lamp" in entities or any(entity in entities for entity in {"light", "light bulb", "sun", "sunlight", "fire", "candle", "screen"}):
        return True
    if "window" not in entities:
        return True
    if re.search(r"\b(window|sunlight|daylight)\s+(?:is|appears|looks|provides|enters|shines|visible)\b", lower):
        return True
    if re.search(r"\b(in front of|through|from|near|beside|on the right|on the left)\s+(?:a\s+|the\s+)?window\b", lower):
        return True
    return not any(re.search(rf"\b{re.escape(term)}s?\b", lower) for term in LIGHT_CONTEXT_EXCLUSION_TERMS)


def useful_action_sentence(sentence: str) -> bool:
    lower = str(sentence or "").lower()
    if not any(re.search(rf"\b{re.escape(term)}\b", lower) for term in PERSON_TERMS):
        return False
    verbs = action_verbs(lower)
    if verbs.intersection({"sit", "read", "hold", "use", "ride", "write", "type", "eat", "stand", "walk", "run", "cut", "prepare", "fight", "argue"}):
        return True
    return any(re.search(rf"\b{re.escape(entity)}s?\b", lower) for entity in ACTION_RELEVANT_ENTITIES)


def action_sentence_entities(sentence: str, selected_entities: set[str]) -> set[str]:
    lower = str(sentence or "").lower()
    entities: set[str] = set()
    if any(re.search(rf"\b{re.escape(term)}\b", lower) for term in PERSON_TERMS):
        entities.add("person")
    for label in selected_entities:
        if label == "person":
            continue
        if re.search(rf"\b{re.escape(label)}s?\b", lower):
            entities.add(label)
    return entities


def best_action_observation(curated: dict[str, Any], question: str = "") -> dict[str, Any] | None:
    observations = [
        item
        for item in curated.get("direct_observations") or []
        if isinstance(item, dict) and useful_action_sentence(str(item.get("text") or ""))
    ]
    if not observations:
        return None
    q_actors = actor_terms_for_question(question)
    def score(item: dict[str, Any]) -> int:
        text = str(item.get("text") or "").lower()
        verbs = action_verbs(text)
        actors = actor_terms_for_text(text)
        primary_actor = primary_actor_for_text(text)
        actor_score = actor_match_score(q_actors, actors, primary_actor)[0] if q_actors else 0
        return (
            len(verbs) * 4
            + sum(2 for entity in ACTION_RELEVANT_ENTITIES if re.search(rf"\b{re.escape(entity)}s?\b", text))
            + (3 if "person" in item.get("entities", []) else 0)
            + actor_score
        )
    return sorted(observations, key=score, reverse=True)[0]


def visual_evidence_text(curated: dict[str, Any]) -> str:
    parts: list[str] = []
    for key in ("retrieved_visual_snippets", "direct_observations", "allowed_inferences"):
        for item in curated.get(key) or []:
            if isinstance(item, dict):
                text = str(item.get("text") or "").strip()
                if text:
                    parts.append(text)
    return " ".join(parts)


def action_verbs(text: str) -> set[str]:
    lower = str(text or "").lower()
    verbs: set[str] = set()
    for word, canonical in ACTION_VERB_ALIASES.items():
        if re.search(rf"\b{re.escape(word)}\b", lower):
            verbs.add(canonical)
    return verbs


def guard_action_verbs(text: str) -> set[str]:
    verbs = action_verbs(text)
    lower = str(text or "").lower()
    if re.search(r"\b(?:does not|doesn't|do not|not|no evidence|does not show|do not show|not show|no sign of)[^.]{0,80}\b(?:fight|fighting|argue|arguing)\b", lower):
        verbs.discard("fight")
        verbs.discard("argue")
    return verbs


def question_actor_targets_from_curated(curated: dict[str, Any]) -> set[str]:
    metadata = curated.get("retrieval_metadata") if isinstance(curated.get("retrieval_metadata"), dict) else {}
    actors = {str(item) for item in metadata.get("question_actor_targets") or [] if str(item) in {"man", "woman", "person", "people"}}
    if actors:
        return actors
    return {str(item) for item in curated.get("question_targets") or [] if str(item) in {"man", "woman", "person", "people"}}


def top_retrieved_action_snippet(curated: dict[str, Any]) -> dict[str, Any] | None:
    for item in curated.get("retrieved_visual_snippets") or []:
        if not isinstance(item, dict):
            continue
        actions = {str(action) for action in item.get("actions") or []}
        if actions:
            return item
    return None


def first_action_object(text: str, verb: str) -> str:
    match = re.search(rf"\b{re.escape(verb)}\s+(?:a|an|the)?\s*([a-z][a-z -]{{1,32}})", text)
    if not match:
        return ""
    phrase = re.sub(r"\b(?:while|with|near|on|in|at|and)\b.*$", "", match.group(1)).strip()
    return phrase


def object_observation_text(item: dict[str, Any], question_type: str, selected_entities: set[str]) -> str:
    label = str(item["canonical_label"])
    count = int(item.get("count") or 1)
    noun = pluralize(label, count) if count > 1 else article(label)
    location = location_phrase(item.get("box"))
    if question_type == "source_or_cause" and label == "lamp" and "desk" in selected_entities:
        return f"{noun} is visible near the desk{location}."
    return f"{noun} is visible{location}."


def article(label: str) -> str:
    return f"an {label}" if label[:1] in {"a", "e", "i", "o", "u"} else f"a {label}"


def pluralize(label: str, count: int) -> str:
    if count == 2:
        prefix = "two"
    else:
        prefix = "several"
    if label.endswith("y"):
        return f"{prefix} {label[:-1]}ies"
    if label.endswith("s"):
        return f"{prefix} {label}"
    return f"{prefix} {label}s"


def location_phrase(box: Any) -> str:
    if not box:
        return ""
    x1, y1, x2, y2 = box
    cx = (x1 + x2) / 2
    cy = (y1 + y2) / 2
    horizontal = " on the left" if cx < 0.33 else " on the right" if cx > 0.66 else ""
    vertical = " near the top" if cy < 0.25 else " near the bottom" if cy > 0.75 else ""
    return f"{horizontal}{vertical}"


def normalized_box(value: Any) -> list[float] | None:
    if not isinstance(value, (list, tuple)) or len(value) < 4:
        return None
    try:
        x1, y1, x2, y2 = [float(item) for item in value[:4]]
    except (TypeError, ValueError):
        return None
    return [max(0.0, min(1.0, item)) for item in (min(x1, x2), min(y1, y2), max(x1, x2), max(y1, y2))]


def box_iou(left: list[float], right: list[float]) -> float:
    lx1, ly1, lx2, ly2 = left
    rx1, ry1, rx2, ry2 = right
    ix1, iy1 = max(lx1, rx1), max(ly1, ry1)
    ix2, iy2 = min(lx2, rx2), min(ly2, ry2)
    if ix2 <= ix1 or iy2 <= iy1:
        return 0.0
    intersection = (ix2 - ix1) * (iy2 - iy1)
    left_area = max(0.0, (lx2 - lx1) * (ly2 - ly1))
    right_area = max(0.0, (rx2 - rx1) * (ry2 - ry1))
    union = left_area + right_area - intersection
    return intersection / union if union else 0.0


def merge_boxes(boxes: list[list[float]]) -> list[float] | None:
    clean = [box for box in boxes if box]
    if not clean:
        return None
    return [
        round(min(box[0] for box in clean), 6),
        round(min(box[1] for box in clean), 6),
        round(max(box[2] for box in clean), 6),
        round(max(box[3] for box in clean), 6),
    ]


def split_sentences(text: str) -> list[str]:
    prepared = re.sub(r"[\r\n]+", ". ", str(text or "").strip())
    return [item.strip() for item in re.split(r"(?<=[.!?])\s+", prepared) if item.strip()]


def dedupe_strings(items: list[str]) -> list[str]:
    seen: set[str] = set()
    result: list[str] = []
    for item in items:
        clean = str(item or "").strip()
        if clean and clean not in seen:
            seen.add(clean)
            result.append(clean)
    return result


def guard_failure(kind: str, reason: str) -> dict[str, Any]:
    return {
        "ok": False,
        "kind": kind,
        "grounding_guard_triggered": True,
        "grounding_guard_reason": reason,
        "regeneration_attempted": False,
        "safe_fallback_used": False,
    }
