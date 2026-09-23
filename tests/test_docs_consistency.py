"""Keep documented defaults honest.

Twice now a number in prose drifted from the code: a docstring claimed "40 tokens" when the
tuple held 45, and the README advertised `AIDEN_MAX_TURNS=10` after the code moved to 12. A live
harness run caught both, which is a slow feedback loop for something a test can pin.

These tests compare the README's environment table against `config`, so changing a default
without changing the docs fails in CI rather than in a user's terminal.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from aiden import config

README = Path(__file__).resolve().parent.parent / "README.md"

#: Documented variable -> the config attribute that is its source of truth.
DOCUMENTED = {
    "AIDEN_MODEL": lambda: config.DEFAULT_MODEL,
    "AIDEN_MAX_TOKENS": lambda: str(config.DEFAULT_MAX_TOKENS),
    "AIDEN_THINKING": lambda: config.DEFAULT_THINKING_LEVEL,
    "AIDEN_MAX_TURNS": lambda: str(config.MAX_TURNS),
    "AIDEN_NUDGE_TURNS": lambda: str(config.NUDGE_TURNS_REMAINING),
    "AIDEN_MAX_COST_USD": lambda: str(config.MAX_RUN_COST_USD),
}

#: Documented variable -> config attribute holding a Path (compared loosely).
DOCUMENTED_PATHS = {
    "AIDEN_HOME": lambda: config.AIDEN_HOME,
    "AIDEN_SESSIONS_DIR": lambda: config.SESSIONS_DIR,
    "AIDEN_SPILL_DIR": lambda: config.SPILL_DIR,
    "AIDEN_AUTH_FILE": lambda: config.AUTH_FILE,
}


def readme_env_table() -> dict[str, str]:
    """Extract ``| `VAR` | `value` | description |`` rows from the README."""
    rows: dict[str, str] = {}
    for line in README.read_text().splitlines():
        match = re.match(r"\|\s*`([A-Z][A-Z0-9_/ ]+)`\s*\|\s*`?([^|`]+)`?\s*\|", line)
        if match:
            rows[match.group(1).strip()] = match.group(2).strip()
    return rows


def test_readme_documents_every_required_variable():
    table = readme_env_table()
    missing = [name for name in (*DOCUMENTED, *DOCUMENTED_PATHS) if name not in table]
    assert not missing, f"undocumented env vars: {missing}"


@pytest.mark.parametrize("name", sorted(DOCUMENTED))
def test_documented_default_matches_config(name: str):
    documented = readme_env_table()[name]
    actual = DOCUMENTED[name]()
    # Compare numerically when both sides are numbers, so 0.50 == 0.5 and 12 == 12.
    try:
        equal = float(documented) == float(actual)
    except ValueError:
        equal = documented == actual
    assert equal, f"README documents {name}={documented!r} but config has {actual!r}"


@pytest.mark.parametrize("name", sorted(DOCUMENTED_PATHS))
def test_documented_paths_are_under_aiden_home(name: str):
    documented = readme_env_table()[name]
    actual = DOCUMENTED_PATHS[name]()
    assert actual.is_absolute(), f"{name} should be absolute"
    # Paths are shown with the home abbreviated; just require the same final component.
    assert documented.split("/")[-1] == actual.name or "$AIDEN_HOME" in documented


def test_readme_token_count_claim_is_not_hardcoded():
    """The theme docstring burned us once; the README must not repeat a count either."""
    from aiden.tui.theme import TOKENS

    text = README.read_text()
    for number in (len(TOKENS), len(TOKENS) - 1):
        assert f"{number} colour tokens" not in text, (
            "README states a hardcoded token count; it will drift"
        )
