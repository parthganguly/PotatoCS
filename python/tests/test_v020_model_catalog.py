from __future__ import annotations

from odysseus_desktop_backend.services.model_service import (
    build_conversation_model_catalog,
    canonical_model_tag,
    display_model_name,
    historical_model_label,
)


def test_canonical_tags_dedupe_latest_alias_and_preserve_sized_tag():
    catalog = build_conversation_model_catalog(
        ["foo", "foo:latest", "llama3.2:latest", "llama3.2:1b"],
        capabilities=[
            {"model": "foo:latest", "text_generation": "yes", "vision": "no", "embedding": "no"},
            {"model": "llama3.2:latest", "text_generation": "yes", "vision": "no", "embedding": "no"},
            {"model": "llama3.2:1b", "text_generation": "yes", "vision": "no", "embedding": "no"},
        ],
    )

    tags = {item["tag"] for item in catalog}
    canonical_tags = {item["canonical_tag"] for item in catalog}

    assert canonical_model_tag("foo") == "foo:latest"
    assert tags == {"foo:latest", "llama3.2:latest", "llama3.2:1b"}
    assert canonical_tags == {"foo:latest", "llama3.2:latest", "llama3.2:1b"}
    assert next(item for item in catalog if item["tag"] == "foo:latest")["exact_tags"] == ["foo", "foo:latest"]


def test_embedding_only_models_are_excluded_and_chat_vision_models_are_allowed():
    catalog = build_conversation_model_catalog(
        ["nomic-embed-text:latest", "qwen3-vl:2b", "qwen3:8b"],
        capabilities=[
            {"model": "nomic-embed-text:latest", "text_generation": "no", "vision": "no", "embedding": "yes"},
            {"model": "qwen3-vl:2b", "text_generation": "yes", "vision": "yes", "embedding": "no"},
            {"model": "qwen3:8b", "text_generation": "yes", "vision": "no", "embedding": "no"},
        ],
    )

    tags = {item["tag"] for item in catalog}
    roles = {item["tag"]: item["role"] for item in catalog}

    assert "nomic-embed-text:latest" not in tags
    assert roles["qwen3-vl:2b"] == "vision"
    assert roles["qwen3:8b"] == "chat"


def test_unknown_embedding_name_is_not_promoted_to_chat():
    catalog = build_conversation_model_catalog(["nomic-embed-text:latest", "unknown-chat:latest"])

    tags = {item["tag"] for item in catalog}

    assert "nomic-embed-text:latest" not in tags
    assert "unknown-chat:latest" in tags


def test_display_names_and_stale_historical_labels():
    installed = ["llama3.2:latest", "llama3.2:1b", "qwen3-vl:2b"]

    assert display_model_name("qwen3:8b") == "Qwen3 8B"
    assert display_model_name("qwen3-vl:2b") == "Qwen3 VL 2B"
    assert display_model_name("llama3.2:1b") == "Llama 3.2 1B"
    assert historical_model_label("openhermes:v2.5", installed) == "OpenHermes V2.5 · not installed"
    assert historical_model_label("llama3.2", installed) == "Llama 3.2"
