from __future__ import annotations

import json
from pathlib import Path

import pytest

from aiden.providers.catalog import DATA_DIR, Catalog
from aiden.providers.registry import ProviderRegistry


@pytest.fixture(scope="module")
def catalog() -> Catalog:
    return Catalog.load()


def test_vendored_catalog_loads(catalog: Catalog):
    stats = catalog.stats()
    assert stats["providers"] >= 40
    assert stats["models"] > 1000


def test_required_providers_present(catalog: Catalog):
    for provider in ("anthropic", "openai", "opencode", "opencode-go", "deepseek", "commandcode"):
        assert provider in catalog.providers, provider
        assert catalog.providers[provider].models, provider


def test_manifest_records_provenance():
    manifest = json.loads((DATA_DIR / ".manifest.json").read_text())
    assert manifest["generator"].endswith("sync_catalog.py")
    assert manifest["commandcode"]["apiBase"].endswith("/provider/v1")


def test_resolve_with_slash_in_model_id(catalog: Catalog):
    model = catalog.resolve("commandcode/meta/muse-spark-1.3-contributor")
    assert model.provider == "commandcode"
    assert model.id == "meta/muse-spark-1.3-contributor"
    assert model.api == "openai-completions"


def test_commandcode_claude_uses_anthropic_protocol(catalog: Catalog):
    model = catalog.resolve("commandcode/claude-sonnet-5")
    assert model.api == "anthropic-messages"
    # anthropic-messages posts to {base}/v1/messages, so base must not end in /v1
    assert model.base_url == "https://api.commandcode.ai/provider"


def test_base_urls_are_normalised(catalog: Catalog):
    for provider in catalog.providers.values():
        for model in provider.models.values():
            assert not model.base_url.endswith("/"), model.ref


def test_unknown_model_suggests_close_match(catalog: Catalog):
    with pytest.raises(KeyError) as exc:
        catalog.get("anthropic", "claude-sonnet-4-5x")
    assert "did you mean" in str(exc.value)


def test_unknown_provider_lists_known_ones(catalog: Catalog):
    with pytest.raises(KeyError) as exc:
        catalog.provider("nope")
    assert "anthropic" in str(exc.value)


def test_resolve_bare_model_id_when_unambiguous(catalog: Catalog):
    model = catalog.resolve("deepseek-flash")
    assert model.provider == "deepseek"


def test_search_matches_name_and_id(catalog: Catalog):
    hits = catalog.search("muse-spark")
    assert hits
    assert all("muse-spark" in m.id for m in hits)


def test_thinking_suffix_parsed(catalog: Catalog):
    registry = ProviderRegistry(catalog)
    resolved = registry.resolve("anthropic/claude-opus-4-8:high")
    assert resolved.model.id == "claude-opus-4-8"
    assert resolved.thinking_level == "high"


def test_model_id_containing_colon_not_mistaken_for_level(catalog: Catalog):
    registry = ProviderRegistry(catalog)
    # '20250929' is not a thinking level, so it stays part of the model id and lookup
    # fails on the id rather than silently resolving a different model.
    with pytest.raises(KeyError):
        registry.resolve("anthropic/claude-sonnet-4-6:20250929")
    resolved = registry.resolve("anthropic/claude-sonnet-4-6")
    assert resolved.thinking_level is None


def test_missing_protocols_reported_not_raised(catalog: Catalog):
    registry = ProviderRegistry(catalog)
    missing = registry.missing_protocols()
    assert "google-generative-ai" in missing  # deferred by design


def test_catalog_override_file_merges(tmp_path: Path):
    override = tmp_path / "providers.toml"
    override.write_text(
        """
[providers.local-llama]
api = "openai-completions"
baseUrl = "http://localhost:11434/v1"

[[providers.local-llama.models]]
id = "llama3.1:8b"
name = "Llama 3.1 8B"
contextWindow = 128000
maxTokens = 32000
"""
    )
    catalog = Catalog.load(override_paths=[override])
    model = catalog.get("local-llama", "llama3.1:8b")
    assert model.api == "openai-completions"
    assert model.base_url == "http://localhost:11434/v1"
    assert model.context_window == 128_000
    # vendored providers survive the merge
    assert "anthropic" in catalog.providers
