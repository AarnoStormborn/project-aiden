"""Provider/model catalog.

Vendored snapshot of pi's generated catalog (``pi-ai/dist/providers/data/*.json``) plus
entries we author ourselves (``commandcode``), plus optional user overrides.

Layout on disk, matching upstream so ``sync`` is a copy:

    data/<provider>.json          {"<api>": {"<model-id>": {ModelInfo fields...}}}
    data/.manifest.json           {"generatedAt": "...", "source": "..."}

User overrides live in ``providers.toml`` (see :func:`load_overrides`) and are *merged*,
so a new provider or a model fix does not require touching vendored files.
"""

from __future__ import annotations

import json
import os
import tomllib
from pathlib import Path
from typing import Any

from .types import Cost, ModelInfo, ProviderInfo

DATA_DIR = Path(__file__).parent / "data"
AIDEN_HOME = Path(os.environ.get("AIDEN_HOME", Path.home() / ".aiden"))


def _model_from_raw(provider: str, raw: dict[str, Any]) -> ModelInfo:
    return ModelInfo(
        id=str(raw.get("id") or ""),
        provider=str(raw.get("provider") or provider),
        api=str(raw.get("api") or ""),
        base_url=str(raw.get("baseUrl") or "").rstrip("/"),
        name=str(raw.get("name") or raw.get("id") or ""),
        reasoning=bool(raw.get("reasoning", False)),
        input_modalities=tuple(raw.get("input") or ("text",)),
        context_window=int(raw.get("contextWindow") or 0),
        max_tokens=int(raw.get("maxTokens") or 0),
        cost=Cost.from_catalog(raw.get("cost")),
        compat=dict(raw.get("compat") or {}),
        thinking_level_map=dict(raw.get("thinkingLevelMap") or {}),
        headers={k: str(v) for k, v in (raw.get("headers") or {}).items()},
    )


class Catalog:
    """In-memory provider/model registry built from vendored data + overrides."""

    def __init__(self, providers: dict[str, ProviderInfo], source: str = ""):
        self.providers = providers
        self.source = source

    # ------------------------------------------------------------------ construct

    @classmethod
    def load(
        cls,
        data_dir: Path | None = None,
        override_paths: list[Path] | None = None,
    ) -> Catalog:
        data_dir = data_dir or DATA_DIR
        providers: dict[str, ProviderInfo] = {}

        if data_dir.is_dir():
            for path in sorted(data_dir.glob("*.json")):
                if path.name.startswith("."):
                    continue
                try:
                    raw = json.loads(path.read_text())
                except Exception as exc:  # pragma: no cover - corrupt vendor file
                    raise ValueError(f"catalog file {path} is not valid JSON: {exc}") from exc
                _merge_provider_file(providers, path.stem, raw)

        source = "vendored"
        for path in override_paths or _default_override_paths():
            if path.is_file():
                _merge_provider_file(providers, None, _load_override_file(path))
                source = f"{source}+{path.name}"

        return cls(providers, source=source)

    # ------------------------------------------------------------------ query

    def provider(self, provider_id: str) -> ProviderInfo:
        try:
            return self.providers[provider_id]
        except KeyError:
            known = ", ".join(sorted(self.providers)) or "<none>"
            raise KeyError(f"unknown provider '{provider_id}'. known: {known}") from None

    def get(self, provider_id: str, model_id: str) -> ModelInfo:
        prov = self.provider(provider_id)
        try:
            return prov.models[model_id]
        except KeyError:
            close = _closest(model_id, prov.models)
            hint = f" did you mean '{close}'?" if close else ""
            raise KeyError(f"unknown model '{model_id}' for {provider_id}.{hint}") from None

    def resolve(self, ref: str) -> ModelInfo:
        """Resolve ``provider/model`` (model ids may themselves contain ``/``)."""
        if "/" not in ref:
            matches = [m for p in self.providers.values() for m in p.models.values() if m.id == ref]
            if len(matches) == 1:
                return matches[0]
            if not matches:
                raise KeyError(f"no model matches '{ref}'")
            raise KeyError(
                f"'{ref}' is ambiguous: " + ", ".join(sorted(m.ref for m in matches)[:8])
            )
        provider_id, _, model_id = ref.partition("/")
        self.provider(provider_id)
        return self.get(provider_id, model_id)

    def search(self, pattern: str) -> list[ModelInfo]:
        """Case-insensitive substring match over ``provider/model`` and names."""
        needle = pattern.lower()
        out = [
            m
            for p in self.providers.values()
            for m in p.models.values()
            if needle in m.ref.lower() or needle in m.id.lower() or needle in (m.name or "").lower()
        ]
        return sorted(out, key=lambda m: (m.provider, m.id))

    def providers_with_models(self) -> list[ProviderInfo]:
        return sorted(
            (p for p in self.providers.values() if p.models),
            key=lambda p: p.id,
        )

    def stats(self) -> dict[str, int]:
        return {
            "providers": len(self.providers),
            "models": sum(len(p.models) for p in self.providers.values()),
        }


# --------------------------------------------------------------------------- internals


def _merge_provider_file(
    providers: dict[str, ProviderInfo],
    provider_hint: str | None,
    raw: dict[str, Any],
) -> None:
    """Merge one catalog file: ``{api: {model_id: model}}`` or ``{providers: {...}}``."""
    if "providers" in raw and isinstance(raw["providers"], dict):
        for pid, body in raw["providers"].items():
            _merge_provider_body(providers, pid, body)
        return
    if provider_hint is None:
        return
    _merge_provider_body(providers, provider_hint, {"apis": raw})


def _merge_provider_body(
    providers: dict[str, ProviderInfo], provider_id: str, body: dict[str, Any]
) -> None:
    prov = providers.setdefault(provider_id, ProviderInfo(id=provider_id))

    if "models" in body and isinstance(body["models"], list):
        # User-style flat list: models inherit provider-level api/baseUrl/headers/compat.
        defaults: dict[str, Any] = {
            key: body[key] for key in ("api", "baseUrl", "headers", "compat") if key in body
        }
        for entry in body["models"]:
            if not isinstance(entry, dict):
                continue
            model = _model_from_raw(provider_id, {**defaults, **entry})
            if model.api:
                prov.models[model.id] = model
        return

    apis = body.get("apis") if "apis" in body else body
    if not isinstance(apis, dict):
        return

    for api, models in apis.items():
        if not isinstance(models, dict):
            continue
        if api == "models":
            continue
        for model_id, entry in models.items():
            if not isinstance(entry, dict):
                continue
            merged = {"id": model_id, "api": api, **entry}
            model = _model_from_raw(provider_id, merged)
            if not model.api:
                continue
            prov.models[model.id] = model


def _default_override_paths() -> list[Path]:
    candidates = [
        Path.cwd() / "providers.toml",
        AIDEN_HOME / "providers.toml",
    ]
    extra = os.environ.get("AIDEN_PROVIDERS_FILE")
    if extra:
        candidates.insert(0, Path(extra))
    return candidates


def _load_override_file(path: Path) -> dict[str, Any]:
    if path.suffix == ".json":
        return json.loads(path.read_text())
    with path.open("rb") as fh:
        return tomllib.load(fh)


def _closest(needle: str, candidates: dict[str, Any]) -> str | None:
    best, best_score = None, 0.0
    for candidate in candidates:
        score = _ratio(needle, candidate)
        if score > best_score:
            best, best_score = candidate, score
    return best if best_score >= 0.6 else None


def _ratio(a: str, b: str) -> float:
    """Dependency-free similarity (difflib is fine here, but keep it explicit)."""
    from difflib import SequenceMatcher

    return SequenceMatcher(None, a, b).ratio()
