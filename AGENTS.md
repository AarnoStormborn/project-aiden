# AGENTS.md

Conventions for humans and coding agents working in this repo.

## What this project is

Aiden is a learning-oriented coding + research harness in Python 3.12+, orchestrated around
the `pi` coding agent. Read `README.md` for status, then the docs layer you need:

| Question | Read |
|---|---|
| why is this built this way? (evidence) | `docs/research/` |
| what are the decisions? (architecture) | `docs/architecture/aiden-architecture.md` |
| what am I building next? | `docs/plan/` |

`docs/BRIEF.md` is the shared brief the six research agents worked from. It defines the
output contract for research docs — follow it if you add one.

## Commands

```bash
uv sync                                  # install (Python 3.12 via uv)
uv run python scripts/dev_setup.py       # macOS: un-hide the editable .pth files (see below)
uv run python -m pytest -q               # tests: no network, must stay fast
uv run ruff check .                      # lint
uv run ruff format .                     # format
uv run mypy                              # types (config in pyproject.toml)
```

> On macOS, `uv sync` writes its editable `.pth` files with `UF_HIDDEN` set, and CPython 3.12+
> skips hidden `.pth` files, so `uv run aiden …` may fail with `ModuleNotFoundError`. Run
> `scripts/dev_setup.py` after syncing, or use `uv run python -m aiden …`, which is unaffected.

`ruff`, `mypy` and `pytest` together are the **Tier-2 self-update gate**
(`docs/architecture/aiden-architecture.md` §4.5). They must stay green: an agent that can
propose patches to itself needs a gate that already exists.

Live, network-and-money checks are opt-in and never part of the test suite:

```bash
uv run python scripts/smoke_providers.py
uv run python scripts/smoke_tools.py
uv run python scripts/acceptance_v01.py
```

Acceptance asserts **correctness only**. Efficiency is reported, not asserted: repeated runs of
the same question varied between 11 and 17 tool calls, so a single-sample bound would be flaky.
Efficiency gates need median-of-3 paired runs (`docs/research/05-benchmarks-evals.md`).

## Hard rules

1. **Never commit credentials.** `~/.aiden/auth.json` (0600) holds them; `providers.toml` is
   gitignored because it can too. Do not print a key — `Credential.display` redacts.
2. **Every constant has a source.** `aiden/config.py` documents why each number exists.
   Numbers copied from a harness without justification are a bug.
3. **Errors are values, not exceptions, at the tool/provider boundary.** A failure the model
   can read and correct must be returned as a message; only genuinely fatal conditions
   propagate.
4. **Caps leave traces.** If output is truncated, a budget is exhausted, or a tool call is
   dropped, say so explicitly (`Stop.message`, `Completion.diagnostic`). Silent truncation is
   a named failure mode (`docs/research/02-tooling-efficiency.md` §3).
5. **Do not edit another agent's file.** Research docs are one owner per file.
6. **Sessions live outside the repo.** `~/.aiden/sessions/` — never write transcripts into the
   working tree.
7. **Tests do not touch the network.** Record fixtures instead (see `tests/fixtures/*.sse`).

## Adding a provider

Usually **no code** — add a catalog entry:

```toml
# providers.toml  (repo root, or ~/.aiden/providers.toml) — gitignored
[providers.local-llama]
api = "openai-completions"
baseUrl = "http://localhost:11434/v1"

[[providers.local-llama.models]]
id = "llama3.1:8b"
contextWindow = 128000
maxTokens = 32000
```

A new *wire protocol* needs one adapter in `aiden/providers/transports/` plus one line in
`TRANSPORTS`. Provider-level requirements that are not model `compat` flags (e.g. opencode's
required `x-opencode-session` routing header) belong in `aiden/providers/capabilities.py`.

After changing the vendored catalog: `uv run python scripts/sync_catalog.py` (add `--check`
to report drift). `aiden/providers/data/` is generated — do not hand-edit it.

## Style

- Python 3.12, `from __future__ import annotations`, dataclasses with `slots=True` for the IR.
- `ruff` line length 100. Type-annotate public functions; mypy runs over `aiden/`.
- Prefer the standard library; add a dependency only with a reason.
- Comments explain *why*, and cite the doc/source that justifies the number or the choice.
- Test names state the invariant (`test_overflow_compacts_rather_than_retries`), not the
  function under test.
