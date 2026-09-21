"""Config and storage-layout tests."""

from __future__ import annotations

import re
from pathlib import Path

from aiden import config
from aiden.config import project_slug, session_dir


def test_project_slug_matches_pi_convention():
    slug = project_slug(Path("/Users/me/code/app"))
    assert slug == "--Users-me-code-app--"


def test_project_slug_is_a_single_path_segment():
    slug = project_slug(Path("/tmp/a/b/c"))
    assert "/" not in slug
    assert slug.startswith("--") and slug.endswith("--")


def test_session_dir_is_under_the_sessions_root():
    path = session_dir(Path("/tmp/proj"))
    assert path.parent == config.SESSIONS_DIR


def test_session_id_is_sortable_and_unique():
    ids = [config.new_session_id() for _ in range(50)]
    assert len(set(ids)) == 50
    assert all(re.fullmatch(r"\d{8}T\d{6}-[0-9a-f]{8}", i) for i in ids)
    # Same-second ids share a timestamp prefix (so they group/sort by time); the random
    # suffix guarantees uniqueness, so within one second ordering is not insertion order.
    assert len({i[:15] for i in ids}) == 1
    assert len({i[16:] for i in ids}) == 50


def test_tool_budgets_are_tighter_than_pis_documented_defaults():
    # research/02 §3: pi ships 50 KB / 2000 lines; Aiden deliberately tightens.
    assert config.READ_MAX_BYTES < 50 * 1024
    assert config.READ_MAX_LINES < 2_000


def test_default_model_is_a_resolvable_reference():
    from aiden.providers import ProviderSuite

    model = ProviderSuite.load().resolve(config.DEFAULT_MODEL)
    assert model.provider and model.id
    assert model.base_url


def test_default_thinking_level_is_a_known_level():
    assert config.DEFAULT_THINKING_LEVEL in (
        "off",
        "minimal",
        "low",
        "medium",
        "high",
        "xhigh",
        "max",
    )


def test_cost_and_turn_limits_are_finite():
    assert 0 < config.MAX_RUN_COST_USD < 100
    assert 0 < config.MAX_TURNS < 1_000


def test_ensure_dirs_creates_home_sessions_and_spill(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "AIDEN_HOME", tmp_path / "home")
    monkeypatch.setattr(config, "SESSIONS_DIR", tmp_path / "home" / "sessions")
    monkeypatch.setattr(config, "SPILL_DIR", tmp_path / "home" / "spill")
    config.ensure_dirs()
    assert (tmp_path / "home" / "sessions").is_dir()
    assert (tmp_path / "home" / "spill").is_dir()


def test_storage_defaults_stay_outside_the_repo():
    """Sessions must not live in the working tree (they could be committed)."""
    assert "project-aiden" not in str(config.SESSIONS_DIR)
    assert config.SESSIONS_DIR.is_absolute()
