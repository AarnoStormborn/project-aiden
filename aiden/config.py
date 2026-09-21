"""All Aiden constants, each with the source that justifies it.

The architecture doc is explicit that this file exists so numbers are auditable rather than
scattered: ``config.py  # ALL constants, each with a source comment``
(docs/architecture/aiden-architecture.md §1 package layout).

Sources referenced below:
  [r02] docs/research/02-tooling-efficiency.md  — tool budgets, token economics
  [r01] docs/research/01-harness-anatomy.md     — loop rules, context lifecycle, cost
  [r09] docs/research/09-measured-evidence.md   — consolidated numbers
  [arch] docs/architecture/aiden-architecture.md
"""

from __future__ import annotations

import os
import uuid
from datetime import UTC, datetime
from pathlib import Path

# --------------------------------------------------------------------------- identity

APP_NAME = "aiden"

#: Distribution/pyproject version is the product version; this is the *harness* version
#: recorded on every session and eval run so a learning-driven change is reproducible
#: ([arch] §4.5: "Every eval run records state_hash + harness_version").
HARNESS_VERSION = "0.1.0"

#: Session-log entry schema version. Version field first, migration second ([arch] §7).
ENTRY_VERSION = 1

# --------------------------------------------------------------------------- storage

AIDEN_HOME = Path(os.environ.get("AIDEN_HOME", Path.home() / ".aiden"))

#: Sessions live outside the repo (like pi) so transcripts can never be committed
#: accidentally. ``AIDEN_SESSIONS_DIR`` overrides for tests and throwaway runs.
SESSIONS_DIR = Path(os.environ.get("AIDEN_SESSIONS_DIR", AIDEN_HOME / "sessions"))

#: Oversized tool output is written here and referenced by path ([r02] §3: "spill-to-file
#: with the path in the message").
SPILL_DIR = Path(os.environ.get("AIDEN_SPILL_DIR", AIDEN_HOME / "spill"))

AUTH_FILE = Path(os.environ.get("AIDEN_AUTH_FILE", AIDEN_HOME / "auth.json"))


def project_slug(cwd: Path | None = None) -> str:
    """Directory name for a project's sessions, mirroring pi's ``--path--`` convention.

    ``/Users/me/code/app`` → ``--Users-me-code-app--``. Keeps sessions grouped per project
    while staying a single, filesystem-safe path segment.
    """
    path = (cwd or Path.cwd()).resolve()
    return "--" + str(path).strip("/").replace("/", "-") + "--"


def session_dir(cwd: Path | None = None) -> Path:
    return SESSIONS_DIR / project_slug(cwd)


def new_session_id() -> str:
    """Time-ordered id used for the JSONL filename, opencode routing and eval runs.

    ``YYYYMMDDTHHMMSS-<8 hex>``: lexicographically sortable, human-scannable, collision-safe.
    """
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%S")
    return f"{stamp}-{uuid.uuid4().hex[:8]}"


# --------------------------------------------------------------------------- model defaults

#: Chosen for v0.1 dev runs: cheap, 1M context, available via the opencode-go key.
#: Overridable per run with ``--model`` (docs/plan/providers.md §11).
DEFAULT_MODEL = os.environ.get("AIDEN_MODEL", "opencode-go/qwen3.8-flash")

#: Catalog ``maxTokens`` values are 65k to 384k, which is a cost hazard for Q&A. 4k is enough
#: for a grounded answer and forces the truncation diagnostic to fire visibly if not.
DEFAULT_MAX_TOKENS = int(os.environ.get("AIDEN_MAX_TOKENS", 4_096))

#: Reasoning models spend output budget before emitting text — Command Code's Muse Spark
#: needed ~700 tokens to answer "pong" (docs/plan/providers.md §9.3). Off by default for
#: dev runs; raise deliberately, and raise max_tokens with it.
DEFAULT_THINKING_LEVEL = os.environ.get("AIDEN_THINKING", "off")

# --------------------------------------------------------------------------- loop limits

#: [r01] §loop: "explicit max_turns + cost_limit with auto-report on trip"; mini-SWE-agent
#: ships step_limit 250 / cost_limit 3.0. v0.1 is single-question, so the turn cap is small.
MAX_TURNS = int(os.environ.get("AIDEN_MAX_TURNS", 10))

#: Hard per-run spend ceiling, enforced *inside* the loop (not by an external killer)
#: because the loop must be able to stop cleanly and report ([r01] §cost).
MAX_RUN_COST_USD = float(os.environ.get("AIDEN_MAX_COST_USD", 0.50))

# --------------------------------------------------------------------------- tool budgets

#: [r02] §3: pi's own defaults are 50 KB / 2000 lines; Aiden tightens to ~2.5x smaller
#: because one unbounded grep measured ~90k tokens against a 200k window.
READ_MAX_BYTES = 16_384
READ_MAX_LINES = 400
GREP_DEFAULT_MODE = "files-first"
GREP_MAX_HITS = 200
GLOB_MAX_PATHS = 500

# --------------------------------------------------------------------------- context

#: [r01] §context: estimator 4 chars/token, reserve 16384 (Pi), keep_recent 20000 (opencode).
CHARS_PER_TOKEN = 4
RESERVE_TOKENS = 16_384
KEEP_RECENT_TOKENS = 20_000


def ensure_dirs() -> None:
    """Create the runtime directories. Safe to call repeatedly."""
    for path in (AIDEN_HOME, SESSIONS_DIR, SPILL_DIR):
        path.mkdir(parents=True, exist_ok=True)
