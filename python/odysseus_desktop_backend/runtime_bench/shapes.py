"""Fixed benchmark shapes: deterministic prompts and quality checks.

Prompts are fully synthetic (no user data). Outputs are never stored in
artifacts; only the quality-check verdict is. All shapes are designed
for greedy decoding (temperature 0, fixed seed where honored).
"""

from __future__ import annotations

LONG_CONTEXT_CODEWORD = "AZURE-KESTREL-42"
GROUNDED_FEE_TOKENS = ("4,750", "4750")
TINY_TOKEN = "POTATO-OK"
MEDIUM_TOPIC_WORDS = ("lighthouse", "harbor", "ferry", "storm")


def _medium_brief() -> str:
    paragraphs = []
    for index in range(8):
        paragraphs.append(
            f"Section {index + 1}. The coastal town committee reviewed the {MEDIUM_TOPIC_WORDS[index % 4]} "
            "maintenance schedule for the winter season. Volunteers documented supply levels, repair "
            "backlogs, and staffing rotations, and compared them against the previous year's records "
            "to decide which projects should be funded first when the budget meeting convenes."
        )
    return "\n\n".join(paragraphs)


def _long_context_document(target_tokens: int = 4000) -> str:
    filler_sentence = (
        "The archive clerk catalogued another shelf of maritime logbooks, noting arrival dates, "
        "cargo manifests, and weather remarks in the ledger before moving on to the next crate. "
    )
    # ~30 words per sentence; aim for roughly `target_tokens` words of filler.
    repeats = max(1, target_tokens // 30)
    parts = []
    for index in range(repeats):
        parts.append(f"[Entry {index + 1}] " + filler_sentence)
        if index == repeats // 2:
            parts.append(
                f"[Entry {index + 1}b] Confidential note: the recovery codeword for the eastern "
                f"vault is {LONG_CONTEXT_CODEWORD}. It must be quoted exactly when asked. "
            )
    parts.append(
        "\n\nQuestion: What is the recovery codeword for the eastern vault? Reply with the codeword only."
    )
    return "".join(parts)


def _grounded_evidence_prompt() -> str:
    return (
        "You answer using ONLY the provided source excerpts. If the excerpts do not contain the "
        "answer, say you cannot find it.\n\n"
        "[Source 1 - community_hall_minutes.pdf]\n"
        "The committee approved the annual maintenance fee of 4,750 rupees for the community hall, "
        "covering roof repairs and the water tank inspection.\n\n"
        "[Source 2 - garden_rota.txt]\n"
        "The garden rota assigns weekly watering duties to four volunteer households on a rotating "
        "basis, with a reserve volunteer for festival weeks.\n\n"
        "Question: What annual maintenance fee did the committee approve, and what does it cover? "
        "Cite the source name."
    )


BENCHMARK_SHAPES: dict[str, dict] = {
    "tiny": {
        # Phrased as recall rather than an echo instruction: small models
        # (llama3.2:1b at temperature 0) refuse the bare "repeat this
        # token" form, verified live 2026-07-17.
        "prompt": (
            f"The harbor master's callsign is {TINY_TOKEN}. "
            "Question: what is the harbor master's callsign? Reply with the callsign only."
        ),
        "num_predict": 32,
        "description": "tiny prompt / short answer",
    },
    "medium": {
        "prompt": _medium_brief()
        + "\n\nSummarize the committee's winter priorities in about four sentences.",
        "num_predict": 256,
        "description": "medium prompt / medium answer",
    },
    "long_context": {
        "prompt": _long_context_document(),
        "num_predict": 64,
        "description": "long-context retrieval-style prompt",
    },
    "repeat": {
        "prompt": _medium_brief()
        + "\n\nSummarize the committee's winter priorities in about four sentences.",
        "num_predict": 256,
        "description": "repeat prompt for cache/warm-path behavior (run back-to-back)",
    },
    "grounded": {
        "prompt": _grounded_evidence_prompt(),
        "num_predict": 256,
        "description": "fixed PotatoCS source-grounded task",
    },
    "overcommit": {
        "prompt": "Reply with the word READY.",
        "num_predict": 16,
        "description": "failure/overcommit case (options chosen by the experiment)",
    },
}


def quality_check(shape: str, output: str) -> str:
    """Return 'passed' | 'failed' | 'not_applicable' for a shape's output."""
    text = (output or "").strip()
    lowered = text.lower()
    if shape == "tiny":
        return "passed" if TINY_TOKEN.lower() in lowered else "failed"
    if shape in {"medium", "repeat"}:
        hits = sum(1 for word in MEDIUM_TOPIC_WORDS if word in lowered)
        return "passed" if len(text) >= 50 and hits >= 2 else "failed"
    if shape == "long_context":
        return "passed" if LONG_CONTEXT_CODEWORD.lower() in lowered else "failed"
    if shape == "grounded":
        return "passed" if any(token in text for token in GROUNDED_FEE_TOKENS) else "failed"
    if shape == "overcommit":
        return "not_applicable"
    raise ValueError(f"unknown benchmark shape: {shape}")
