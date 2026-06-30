from __future__ import annotations

import json
from pathlib import Path
from typing import Any


REPO_ROOT = Path(__file__).resolve().parents[3]
DEFAULT_SUITE_PATH = REPO_ROOT / "benchmarks" / "vision_common_sense" / "suite.json"
DEFAULT_ROUTES_PATH = REPO_ROOT / "benchmarks" / "vision_common_sense" / "routes.json"


class FixtureValidationError(ValueError):
    pass


def load_suite(
    path: str | Path = DEFAULT_SUITE_PATH,
    *,
    local_image_dir: str | Path | None = None,
    require_images: bool = True,
) -> dict[str, Any]:
    suite_path = Path(path).expanduser().resolve()
    raw = json.loads(suite_path.read_text(encoding="utf-8"))
    if not isinstance(raw, dict):
        raise FixtureValidationError("suite root must be a JSON object")
    suite = {
        "suite_id": require_str(raw, "suite_id"),
        "suite_name": require_str(raw, "suite_name"),
        "suite_version": require_str(raw, "suite_version"),
        "description": str(raw.get("description") or ""),
        "target_shape": raw.get("target_shape") or {},
        "path": str(suite_path),
        "images": {},
        "cases": [],
    }
    image_items = raw.get("images")
    if not isinstance(image_items, list) or not image_items:
        raise FixtureValidationError("suite must include at least one image")
    for image in image_items:
        normalized = normalize_image(image, suite_path.parent, local_image_dir)
        if normalized["id"] in suite["images"]:
            raise FixtureValidationError(f"duplicate image id: {normalized['id']}")
        if require_images and not Path(str(normalized["resolved_path"])).is_file():
            raise FileNotFoundError(f"benchmark image is missing: {normalized['path']}")
        suite["images"][normalized["id"]] = normalized

    cases = raw.get("cases")
    if not isinstance(cases, list) or not cases:
        raise FixtureValidationError("suite must include at least one case")
    for case in cases:
        normalized = normalize_case(case)
        if normalized["image_id"] not in suite["images"]:
            raise FixtureValidationError(f"case references unknown image id: {normalized['image_id']}")
        suite["cases"].append(normalized)
    return suite


def load_routes(path: str | Path = DEFAULT_ROUTES_PATH) -> list[dict[str, Any]]:
    routes_path = Path(path).expanduser().resolve()
    raw = json.loads(routes_path.read_text(encoding="utf-8"))
    routes = raw.get("routes") if isinstance(raw, dict) else raw
    if not isinstance(routes, list) or not routes:
        raise FixtureValidationError("routes file must contain a non-empty routes list")
    seen: set[str] = set()
    normalized: list[dict[str, Any]] = []
    for route in routes:
        if not isinstance(route, dict):
            raise FixtureValidationError("each route must be an object")
        route_id = require_str(route, "route_id")
        if route_id in seen:
            raise FixtureValidationError(f"duplicate route id: {route_id}")
        seen.add(route_id)
        normalized.append(
            {
                "route_id": route_id,
                "label": str(route.get("label") or route_id),
                "vision_backend": str(route.get("vision_backend") or ""),
                "vision_model": str(route.get("vision_model") or ""),
                "final_model": str(route.get("final_model") or ""),
                "multimodal_mode": str(route.get("multimodal_mode") or "automatic"),
                "requires_florence": bool(route.get("requires_florence") or False),
                "requires_ollama_models": [str(item) for item in route.get("requires_ollama_models") or []],
                "requires_vision_capability": bool(route.get("requires_vision_capability") or False),
                "smoke_only": bool(route.get("smoke_only") or False),
                "notes": str(route.get("notes") or ""),
            }
        )
    return normalized


def normalize_image(
    image: Any,
    suite_dir: Path,
    local_image_dir: str | Path | None,
) -> dict[str, Any]:
    if not isinstance(image, dict):
        raise FixtureValidationError("each image must be an object")
    image_id = require_str(image, "id")
    raw_path = require_str(image, "path")
    resolved = resolve_image_path(raw_path, suite_dir, local_image_dir)
    return {
        "id": image_id,
        "path": raw_path,
        "resolved_path": str(resolved),
        "license": require_str(image, "license"),
        "source_note": require_str(image, "source_note"),
        "safe_to_thumbnail": bool(image.get("safe_to_thumbnail") or False),
        "private": bool(image.get("private") or raw_path.replace("\\", "/").startswith("local_images/") or Path(raw_path).is_absolute()),
        "notes": str(image.get("notes") or ""),
    }


def resolve_image_path(raw_path: str, suite_dir: Path, local_image_dir: str | Path | None) -> Path:
    clean = raw_path.replace("\\", "/")
    if clean.startswith("local_images/"):
        root = Path(local_image_dir).expanduser() if local_image_dir else suite_dir / "local_images"
        return (root / clean[len("local_images/") :]).resolve()
    path = Path(raw_path).expanduser()
    if path.is_absolute():
        return path.resolve()
    return (suite_dir / path).resolve()


def normalize_case(case: Any) -> dict[str, Any]:
    if not isinstance(case, dict):
        raise FixtureValidationError("each case must be an object")
    turns = case.get("turns")
    if not isinstance(turns, list) or not turns:
        raise FixtureValidationError(f"case {case.get('id') or '<unknown>'} must include turns")
    case_id = require_str(case, "id")
    normalized_turns = [normalize_turn(turn, case_id, index) for index, turn in enumerate(turns)]
    return {
        "id": case_id,
        "image_id": require_str(case, "image_id"),
        "category": require_str(case, "category"),
        "question_type": str(case.get("question_type") or ""),
        "notes": str(case.get("notes") or ""),
        "human_scorer_notes": str(case.get("human_scorer_notes") or ""),
        "turns": normalized_turns,
    }


def normalize_turn(turn: Any, case_id: str, index: int) -> dict[str, Any]:
    if not isinstance(turn, dict):
        raise FixtureValidationError(f"turn {index + 1} in {case_id} must be an object")
    expected = string_list(turn.get("expected_good") or turn.get("expected_concepts"), "expected_good")
    acceptable = string_list(turn.get("acceptable") or turn.get("acceptable_concepts") or [], "acceptable")
    must_not = string_list(turn.get("must_not_include") or [], "must_not_include")
    return {
        "id": str(turn.get("id") or f"{case_id}_turn_{index + 1}"),
        "question": require_str(turn, "question"),
        "question_type": str(turn.get("question_type") or ""),
        "expected_good": expected,
        "expected_concepts": list(expected),
        "acceptable": acceptable,
        "acceptable_concepts": list(acceptable),
        "must_not_include": must_not,
        "correct_abstention": bool(turn.get("correct_abstention") or False),
        "followup_should_reuse_evidence": bool(turn.get("followup_should_reuse_evidence") if "followup_should_reuse_evidence" in turn else index > 0),
        "notes": str(turn.get("notes") or turn.get("human_notes") or ""),
    }


def require_str(payload: dict[str, Any], key: str) -> str:
    value = payload.get(key)
    if not isinstance(value, str) or not value.strip():
        raise FixtureValidationError(f"{key} must be a non-empty string")
    return value.strip()


def string_list(value: Any, key: str) -> list[str]:
    if not isinstance(value, list) or not value:
        raise FixtureValidationError(f"{key} must be a non-empty list")
    result = [str(item).strip() for item in value if str(item).strip()]
    if not result:
        raise FixtureValidationError(f"{key} must contain at least one non-empty value")
    return result
