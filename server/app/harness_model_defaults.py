from __future__ import annotations

from friday.domain.provider import ModelCatalog, ModelChoice, ModelInfo

# Keep this as a small product preference layer above provider catalogs.
# Values may be either a bare model id ("gpt-5.5") or an OpenCode-style
# provider/model ref ("opencode-go/deepseek-v4-flash").
HARNESS_MODEL_DEFAULTS: dict[str, str] = {
    "opencode": "opencode-go/deepseek-v4-flash",
    "codex": "gpt-5.5",
}


def model_ref(provider_id: str, model_id: str) -> str:
    return f"{provider_id}/{model_id}" if provider_id else model_id


def model_info_ref(model: ModelInfo) -> str:
    return model_ref(model.provider_id, model.model_id)


def model_choice_ref(model: ModelChoice) -> str:
    return model_ref(model.provider_id, model.model_id)


def parse_model_ref(value: str) -> tuple[str | None, str]:
    provider_id, sep, model_id = value.partition("/")
    if not sep:
        return None, value
    return provider_id or None, model_id


def catalog_model_ref(catalog: ModelCatalog, requested: str) -> str | None:
    requested_provider_id, requested_model_id = parse_model_ref(requested)
    for model in catalog.models:
        if requested_provider_id is not None:
            if model.provider_id == requested_provider_id and model.model_id == requested_model_id:
                return model_info_ref(model)
            continue
        if model.model_id == requested_model_id or model_info_ref(model) == requested:
            return model_info_ref(model)
    return None


def default_model_ref(harness: str, catalog: ModelCatalog) -> str | None:
    configured_default = HARNESS_MODEL_DEFAULTS.get(harness)
    if configured_default:
        matched = catalog_model_ref(catalog, configured_default)
        if matched is not None:
            return matched

    if catalog.default is not None:
        matched = catalog_model_ref(catalog, model_choice_ref(catalog.default))
        if matched is not None:
            return matched

    if catalog.models:
        return model_info_ref(catalog.models[0])
    return None
