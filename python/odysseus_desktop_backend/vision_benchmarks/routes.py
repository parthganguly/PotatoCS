from __future__ import annotations

from typing import Any

from odysseus_desktop_backend.services.model_service import canonical_model_tag


def route_by_id(routes: list[dict[str, Any]], route_id: str) -> dict[str, Any]:
    for route in routes:
        if route.get("route_id") == route_id:
            return route
    raise KeyError(f"vision benchmark route not found: {route_id}")


def installed_model_match(required: str, installed: list[str]) -> str:
    required_canonical = canonical_model_tag(required)
    for model in installed:
        if canonical_model_tag(model) == required_canonical:
            return model
    return ""


def check_static_route_availability(
    route: dict[str, Any],
    *,
    installed_models: list[str],
    florence_ready: bool,
    model_capabilities: dict[str, dict[str, Any]] | None = None,
) -> dict[str, Any]:
    if route.get("smoke_only"):
        return {"available": True, "status": "available", "reason": "smoke route uses deterministic fake responses"}
    if route.get("requires_florence") and not florence_ready:
        return {"available": False, "status": "skipped_missing_backend", "reason": "Florence-2 Basic local backend is not ready"}
    required_models = list(route.get("requires_ollama_models") or [])
    final_model = str(route.get("final_model") or "").strip()
    if final_model and final_model not in required_models:
        required_models.append(final_model)
    missing = [model for model in required_models if not installed_model_match(model, installed_models)]
    if missing:
        return {
            "available": False,
            "status": "skipped_missing_model",
            "reason": "Missing local Ollama model(s): " + ", ".join(missing),
        }
    if route.get("requires_vision_capability"):
        capabilities = model_capabilities or {}
        target = final_model or str(route.get("vision_model") or "")
        matched = installed_model_match(target, installed_models)
        capability = capabilities.get(canonical_model_tag(matched)) or capabilities.get(canonical_model_tag(target)) or {}
        if capability and capability.get("vision") != "yes":
            return {
                "available": False,
                "status": "skipped_missing_backend",
                "reason": f"{target} is installed but is not confirmed vision-capable",
            }
    return {"available": True, "status": "available", "reason": ""}
