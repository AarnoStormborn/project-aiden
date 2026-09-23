"""System prompt assembly (L2).

The base prompt is an **immutable, version-controlled file** and is never written by the
agent. Everything the agent later learns goes into *supplemental* state assembled after it
(docs/architecture/aiden-architecture.md §1: "``base_prompt`` is version-controlled and
*never* writable by the agent"). v0.1 therefore has no mutable half yet, but the seam exists
so adding one is not a rewrite.
"""

from __future__ import annotations

import hashlib
from functools import lru_cache
from pathlib import Path

PROMPT_DIR = Path(__file__).parent / "prompts"
BASE_PROMPT_PATH = PROMPT_DIR / "system.md"


@lru_cache(maxsize=1)
def base_prompt() -> str:
    """The immutable base system prompt."""
    return BASE_PROMPT_PATH.read_text(encoding="utf-8").strip()


@lru_cache(maxsize=1)
def base_prompt_hash() -> str:
    """Short content hash, recorded so a behaviour change is attributable to a prompt change.

    [arch] §11: "``harness_version`` + ``state_hash`` on every run" — without this, a
    learning-driven regression is not reproducible.
    """
    return hashlib.sha256(base_prompt().encode()).hexdigest()[:12]


def build(cwd: Path) -> str:
    """Render the system prompt for a working directory."""
    return base_prompt().format(cwd=str(cwd))
