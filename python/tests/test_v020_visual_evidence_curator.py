from __future__ import annotations

from odysseus_desktop_backend.services.visual_evidence_curator import (
    OCR_NO_TEXT_WARNING,
    VISUAL_EVIDENCE_RETRIEVER_VERSION,
    canonicalize_label,
    classify_question,
    compact_synthesis_packet,
    curate_visual_evidence,
    deduplicate_objects,
    deterministic_visual_answer,
    extract_question_targets,
    guard_visual_answer,
    safe_fallback_answer,
    status_from_warnings,
)


def test_labels_canonicalize_and_duplicate_missing_boxes_collapse():
    objects = deduplicate_objects(
        [
            {"label": " Picture Frames ", "source": "object_detection"},
            {"label": "framed picture", "source": "dense_region_caption"},
            {"label": "SNEAKERS", "source": "object_detection"},
            {"label": "shoes", "source": "object_detection"},
        ]
    )

    assert canonicalize_label("Picture Frames") == "picture frame"
    assert [item["canonical_label"] for item in objects] == ["picture frame", "shoe"]
    assert set(objects[0]["raw_labels"]) == {"Picture Frames", "framed picture"}
    assert objects[0]["confidence"] is None


def test_overlapping_boxes_merge_and_separate_boxes_preserve_multiplicity():
    objects = deduplicate_objects(
        [
            {"label": "chair", "box": [0.1, 0.1, 0.4, 0.5], "source": "object_detection"},
            {"label": "chairs", "box": [0.12, 0.12, 0.42, 0.52], "source": "object_detection"},
            {"label": "chair", "box": [0.65, 0.1, 0.9, 0.5], "source": "object_detection"},
        ]
    )

    assert len(objects) == 2
    assert objects[0]["canonical_label"] == "chair"
    assert len(objects[0]["boxes"]) == 2
    assert objects[1]["canonical_label"] == "chair"
    assert len(objects[1]["boxes"]) == 1


def test_question_classifier_and_targets_are_bounded():
    assert classify_question("Where is the light coming from?") == "source_or_cause"
    assert classify_question("What is the man holding?") == "holding_or_contact"
    assert classify_question("What is the man doing?") == "action"
    assert classify_question("What is he doing?") == "action"
    assert classify_question("What are they doing?") == "action"
    assert classify_question("What is the man up to?") == "action"
    assert classify_question("What's he up to?") == "action"
    assert classify_question("What's going on here?") == "action"
    assert classify_question("Describe the activity.") == "action"
    assert classify_question("What object is he using?") == "object_use"
    assert classify_question("What is happening in the image?") == "action"
    assert classify_question("What color is the outerwear?") == "color_attribute"
    assert classify_question("What color is his jacket?") == "color_attribute"
    assert classify_question("What color are his pants?") == "color_attribute"
    assert classify_question("What color is he wearing?") == "color_attribute"
    assert classify_question("What is he wearing?") == "clothing_attribute"
    assert classify_question("Describe his clothing.") == "clothing_attribute"
    assert classify_question("What clothes is he wearing?") == "clothing_attribute"
    assert classify_question("Is there a swimming pool?") == "object_presence"
    assert classify_question("How many chairs are there?") == "object_count"
    assert classify_question("Read the sign") == "text_reading"
    assert classify_question("Who is this person?") == "person_identity"
    assert classify_question("Does it look like an IKEA desk?") == "brand_or_origin"
    assert classify_question("Are they happy?") == "emotion_or_intent"
    assert extract_question_targets("Where is the light coming from?")[:2] == ["light", "illumination"]
    assert "swimming pool" in extract_question_targets("Is there a swimming pool?")


def test_followup_question_type_switches_from_holding_to_clothing_attributes():
    evidence = {
        "schema": "odysseus.visual_evidence.v1",
        "artifact_id": "red-jacket-image",
        "summary": (
            "A man is sitting on a stool. "
            "He is holding two guns in his hands. "
            "He is wearing a red jacket and black pants. "
            "There is a building behind the man."
        ),
        "objects": [{"label": "person", "source": "object_detection"}],
    }

    holding = curate_visual_evidence(evidence, "what is the man holding?")
    outerwear = curate_visual_evidence(evidence, "What color is the outerwear that he is wearing?")
    pants = curate_visual_evidence(evidence, "What color are his pants?")
    clothing = curate_visual_evidence(evidence, "What is he wearing?")
    held_color = curate_visual_evidence(evidence, "What color is the thing he is holding?")

    assert holding["question_type"] == "holding_or_contact"
    assert "two guns" in deterministic_visual_answer("what is the man holding?", holding).lower()
    assert outerwear["question_type"] == "color_attribute"
    assert outerwear["retrieved_visual_snippets"][0]["text"] == "He is wearing a red jacket and black pants."
    assert deterministic_visual_answer("What color is the outerwear that he is wearing?", outerwear) == "His outerwear appears to be red."
    assert deterministic_visual_answer("What color are his pants?", pants) == "His pants appear to be black."
    assert deterministic_visual_answer("What is he wearing?", clothing) == "He appears to be wearing a red jacket and black pants."
    assert deterministic_visual_answer("What color is the thing he is holding?", held_color) == (
        "The image evidence does not clearly state the color of the objects he is holding."
    )
    assert holding["retrieval_metadata"]["cache_key"] != outerwear["retrieval_metadata"]["cache_key"]
    assert holding["retrieval_metadata"]["question_text_hash"] != outerwear["retrieval_metadata"]["question_text_hash"]


def test_attribute_guard_rejects_stale_holding_answer():
    curated = curate_visual_evidence(
        {"summary": "He is holding two guns in his hands. He is wearing a red jacket and black pants."},
        "What color is the outerwear that he is wearing?",
    )

    guard = guard_visual_answer("He is holding two guns in his hands.", curated)

    assert guard["grounding_guard_triggered"] is True
    assert guard["kind"] == "question_type_mismatch"
    assert guard["grounding_guard_reason"] == "answer_repeated_previous_question"


def test_colloquial_action_questions_preserve_direct_action_caption():
    variants = [
        "What is the man doing?",
        "What is the man up to?",
        "What's he up to?",
        "What's she up to?",
        "What are they up to?",
        "What's going on here?",
        "What is happening in the image?",
        "What activity is shown?",
        "Describe the activity.",
        "What is this person doing?",
        "What is the person doing?",
        "What is he doing?",
        "What is she doing?",
        "What are they doing?",
    ]
    evidence = {
        "summary": "A man is sitting in a chair reading a book.",
        "objects": [
            {"label": "person", "source": "object_detection"},
            {"label": "book", "source": "object_detection"},
            {"label": "chair", "source": "object_detection"},
            {"label": "lamp", "source": "object_detection"},
        ],
    }

    for question in variants:
        curated = curate_visual_evidence(evidence, question)
        observed = [item["text"] for item in curated["direct_observations"]]

        assert classify_question(question) == "action", question
        assert curated["question_type"] == "action", question
        assert observed[0] == "A man is sitting in a chair reading a book.", question
        assert "book" not in curated["excluded_irrelevant_entities"], question
        assert "chair" not in curated["excluded_irrelevant_entities"], question
        assert deterministic_visual_answer(question, curated) == (
            "He appears to be sitting in a chair and reading a book."
        ), question


def test_action_caption_preserves_sitting_reading_clause_and_book_relevance():
    evidence = {
        "summary": "A man is sitting in a chair reading a book.",
        "objects": [
            {"label": "person", "source": "object_detection"},
            {"label": "book", "source": "object_detection"},
            {"label": "chair", "source": "object_detection"},
            {"label": "lamp", "source": "object_detection"},
        ],
    }

    curated = curate_visual_evidence(evidence, "What is the man doing?")

    observed = [item["text"] for item in curated["direct_observations"]]
    inference = [item["text"] for item in curated["allowed_inferences"]]
    assert curated["question_type"] == "action"
    assert observed[0] == "A man is sitting in a chair reading a book."
    assert "book" not in curated["excluded_irrelevant_entities"]
    assert "chair" not in curated["excluded_irrelevant_entities"]
    assert "He appears to be reading while seated." in inference


def test_visual_retriever_prefers_man_cutting_over_woman_context():
    evidence = {
        "summary": (
            "A man and a woman are in a kitchen. "
            "The man is cutting vegetables on a cutting board. "
            "The woman is standing next to the man. "
            "A knife and cutting board are visible."
        ),
        "objects": [
            {"label": "person", "source": "object_detection"},
            {"label": "knife", "source": "object_detection"},
            {"label": "cutting board", "source": "object_detection"},
            {"label": "vegetables", "source": "object_detection"},
        ],
    }

    curated = curate_visual_evidence(evidence, "What is the man doing?")
    snippets = curated["retrieved_visual_snippets"]
    answer = deterministic_visual_answer("What is the man doing?", curated)
    guard = guard_visual_answer("The woman is standing next to the man.", curated)
    packet = compact_synthesis_packet("What is the man doing?", curated)

    assert curated["retriever_version"] == VISUAL_EVIDENCE_RETRIEVER_VERSION
    assert snippets[0]["text"] == "The man is cutting vegetables on a cutting board."
    assert snippets[0]["actions"] == ["cut"]
    assert answer == "He appears to be cutting vegetables on a cutting board."
    assert guard["grounding_guard_triggered"] is True
    assert guard["kind"] in {"actor_mismatch", "contradicts_top_visual_snippet"}
    assert "RETRIEVED VISUAL EVIDENCE" in packet
    assert "[V2] The man is cutting vegetables on a cutting board." in packet


def test_visual_retriever_answers_fighting_question_from_absent_evidence():
    evidence = {
        "summary": (
            "A man and a woman are in a kitchen. "
            "The man is cutting vegetables on a cutting board. "
            "The woman is standing next to the man."
        ),
        "objects": [
            {"label": "person", "source": "object_detection"},
            {"label": "knife", "source": "object_detection"},
            {"label": "cutting board", "source": "object_detection"},
        ],
    }

    curated = curate_visual_evidence(evidence, "Are the man and woman fighting?")
    answer = deterministic_visual_answer("Are the man and woman fighting?", curated)
    guard = guard_visual_answer(answer, curated)

    assert curated["question_type"] == "action"
    assert "fighting" in answer.lower()
    assert "does not show" in answer.lower()
    assert "man cutting vegetables" in answer.lower()
    assert "woman standing nearby" in answer.lower()
    assert guard["ok"] is True


def test_stale_generic_curated_evidence_is_replaced_by_raw_snippet_retrieval():
    raw_evidence = {
        "summary": "A man is sitting in a chair reading a book.",
        "objects": [
            {"label": "person", "source": "object_detection"},
            {"label": "book", "source": "object_detection"},
            {"label": "chair", "source": "object_detection"},
        ],
    }
    stale = {"direct_observations": [{"text": "a person is visible"}]}

    curated = curate_visual_evidence(raw_evidence, "What is the person in the picture up to?")
    answer = deterministic_visual_answer("What is the person in the picture up to?", curated)

    assert stale["direct_observations"][0]["text"] not in [
        item["text"] for item in curated["direct_observations"]
    ]
    assert curated["retrieved_visual_snippets"][0]["text"] == "A man is sitting in a chair reading a book."
    assert answer == "He appears to be sitting in a chair and reading a book."


def test_action_deterministic_answer_and_walking_guard():
    curated = curate_visual_evidence(
        {
            "summary": "A man is sitting in a chair reading a book.",
            "objects": [
                {"label": "person", "source": "object_detection"},
                {"label": "book", "source": "object_detection"},
                {"label": "chair", "source": "object_detection"},
            ],
        },
        "What is the man doing?",
    )

    answer = deterministic_visual_answer("What is the man doing?", curated)
    guards = [
        guard_visual_answer("The man is walking.", curated),
        guard_visual_answer("The man is standing still.", curated),
        guard_visual_answer("The man is moving slowly.", curated),
    ]

    assert answer == "He appears to be sitting in a chair and reading a book."
    assert all(guard["grounding_guard_triggered"] is True for guard in guards)
    assert all(guard["kind"] == "unsupported_action" for guard in guards)
    assert "walk" in guards[0]["grounding_guard_reason"]
    assert "stand" in guards[1]["grounding_guard_reason"]
    assert "move" in guards[2]["grounding_guard_reason"]
    assert guard_visual_answer(answer, curated)["ok"] is True


def test_person_identity_fallback_abstains_before_generic_visibility():
    curated = curate_visual_evidence(
        {
            "summary": "A man is sitting in a chair reading a book.",
            "objects": [{"label": "person", "source": "object_detection"}],
        },
        "Who is this man?",
    )

    answer = safe_fallback_answer("Who is this man?", curated)

    assert answer == "I can't identify who he is from the image."
    assert "person is visible" not in answer.lower()


def test_lighting_relevance_selects_lamp_and_window_excludes_picture_frames():
    evidence = {
        "summary": "A lamp is visible beside the desk. A window is visible on the right. Pictures hang on the wall.",
        "objects": [
            {"label": "lamp", "box": [0.1, 0.5, 0.2, 0.7], "source": "object_detection"},
            {"label": "window", "box": [0.7, 0.0, 0.95, 0.4], "source": "object_detection"},
            {"label": "picture frame", "box": [0.3, 0.1, 0.5, 0.3], "source": "object_detection"},
            {"label": "book", "box": [0.2, 0.8, 0.4, 0.9], "source": "object_detection"},
        ],
    }

    curated = curate_visual_evidence(evidence, "Where is the light in the image coming from?")

    observed_text = " ".join(item["text"] for item in curated["direct_observations"])
    inference_text = " ".join(item["text"] for item in curated["allowed_inferences"])
    assert "lamp" in observed_text.lower()
    assert "window" in observed_text.lower()
    assert "lamp" in inference_text.lower()
    assert "window" in inference_text.lower()
    assert "picture frame" in curated["excluded_irrelevant_entities"]
    assert "book" in curated["excluded_irrelevant_entities"]
    assert all(item["supported_by"] for item in curated["allowed_inferences"])
    assert deterministic_visual_answer(
        "Where is the light in the image coming from?",
        curated,
    ) == "The lamp appears to be the main light source. The window may provide secondary ambient daylight."
    role_guard = guard_visual_answer("The light is coming from both the lamp and the window.", curated)
    assert role_guard["grounding_guard_triggered"] is True
    assert role_guard["kind"] == "missing_light_source_role"


def test_daylight_followup_selects_window_from_caption_without_box():
    evidence = {
        "summary": "A man is sitting in front of a window. There is a brown lamp next to the man. Pictures hang on the wall.",
        "objects": [
            {"label": "lamp", "source": "object_detection"},
            {"label": "picture frame", "source": "object_detection"},
        ],
    }

    curated = curate_visual_evidence(evidence, "Does it also look like daylight is entering?")

    observed_text = " ".join(item["text"] for item in curated["direct_observations"]).lower()
    inference_text = " ".join(item["text"] for item in curated["allowed_inferences"]).lower()
    assert curated["question_type"] == "source_or_cause"
    assert "window" in observed_text
    assert "window may provide secondary ambient daylight" in inference_text
    assert inference_text.count("window may provide secondary ambient daylight") == 1
    assert "picture frame" in curated["excluded_irrelevant_entities"]


def test_identity_brand_and_emotion_requests_are_marked_unsupported():
    for question in ("Who is this person?", "Is this an IKEA desk?", "Are they happy?"):
        curated = curate_visual_evidence({"summary": "A person is seated at a desk.", "objects": []}, question)
        assert curated["unsupported_requests"]


def test_compact_packet_omits_raw_duplicate_object_dump():
    evidence = {
        "summary": "A lamp is visible beside the desk. A window is visible on the right.",
        "objects": [
            {"label": "picture frame", "source": "object_detection"},
            {"label": "picture frame", "source": "object_detection"},
            {"label": "lamp", "source": "object_detection"},
            {"label": "window", "source": "object_detection"},
        ],
    }
    curated = curate_visual_evidence(evidence, "Where is the light coming from?")

    packet = compact_synthesis_packet("Where is the light coming from?", curated)

    assert "DIRECTLY OBSERVED" in packet
    assert "SUPPORTED INFERENCE" in packet
    assert packet.count("picture frame") <= 1
    assert "raw" not in packet.lower()


def test_grounding_guard_blocks_excluded_entity_and_safe_fallback_answers():
    curated = curate_visual_evidence(
        {
            "summary": "A lamp is visible beside the desk. A window is visible on the right. Pictures hang on the wall.",
            "objects": [
                {"label": "lamp", "source": "object_detection"},
                {"label": "window", "source": "object_detection"},
                {"label": "picture frame", "source": "object_detection"},
            ],
        },
        "Where is the light coming from?",
    )

    guard = guard_visual_answer("The lamp, window, and picture frames illuminate the room.", curated)
    fallback = safe_fallback_answer("Where is the light coming from?", curated)

    assert guard["grounding_guard_triggered"] is True
    assert "picture frame" in guard["grounding_guard_reason"]
    assert "lamp" in fallback.lower()
    assert "picture frame" not in fallback.lower()


def test_grounding_guard_blocks_daylight_followup_that_ignores_window_target():
    curated = curate_visual_evidence(
        {
            "summary": "A man is sitting in front of a window. There is a brown lamp next to the man.",
            "objects": [{"label": "lamp", "source": "object_detection"}],
        },
        "Does it also look like daylight is entering?",
    )

    guard = guard_visual_answer("The light is coming from the lamp.", curated)
    fallback = safe_fallback_answer("Does it also look like daylight is entering?", curated)

    assert guard["grounding_guard_triggered"] is True
    assert guard["kind"] == "missing_question_target"
    assert "window" in fallback.lower()
    assert "daylight" in fallback.lower()


def test_ocr_no_text_is_informational_for_visual_question_but_warning_for_text_reading():
    assert status_from_warnings([OCR_NO_TEXT_WARNING], "Where is the lamp?") == "completed"
    assert status_from_warnings([OCR_NO_TEXT_WARNING], "Read the text on the sign") == "completed_with_warnings"
    assert status_from_warnings(["object_detection failed"], "Describe the image") == "completed_with_warnings"
