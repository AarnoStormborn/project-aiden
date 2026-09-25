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
uv run python scripts/bench_tui.py --assert-budget   # frame-time budget, no network
```

The TUI's perf budget is enforced in CI by `tests/tui/test_perf.py` (p50 ≤ 3 ms, p99 ≤ 8 ms,
≤ 2 KB/frame streaming, 0 bytes for an unchanged frame). `scripts/bench_tui.py` reports the
distribution when you need detail.

The TUI is snapshot-tested: `AIDEN_UPDATE_GOLDEN=1 uv run python -m pytest tests/tui/` rewrites
the frames in `tests/tui/golden/` at the three widths the spec mandates (80/94/120). Snapshot the
layout before changing it, and treat a golden diff as a question, not an obstacle.

Acceptance asserts **correctness only**. Efficiency is reported, not asserted: repeated runs of
the same question varied between 11 and 17 tool calls, so a single-sample bound would be flaky.
Efficiency gates need median-of-3 paired runs (`docs/research/05-benchmarks-evals.md`).

## Writing files

Aiden can change the working tree through `edit` and `write`. Both go through the same chain:
schema → read-before-edit (content hash) → unique match → path policy → **approval** → apply →
parse gate. Every step returns an error *result* the model can read, never an exception.

Rules that must not be relaxed without updating `docs/plan/v0.2a-write-capability.md`:

1. **A run with nobody to ask cannot write.** Non-interactive runs deny by default; `--allow-writes`
   is the explicit opt-in, and it records `decided_by=flag` so the transcript does not imply the user
   approved it.
2. **Deny beats allow.** `.git/**` is never writable. A hook or a policy must not be able to upgrade
   a decision.
3. **A checkpoint exists before any change**, and it is captured *after* the decision so declined
   proposals leave nothing behind.
4. **Every decision is logged** (`approval` entries), because the transcript is the audit trail.

### Shell commands

`bash` is the most dangerous tool, so its policy is deliberately narrow:

- **Only provably read-only commands run unattended** — `ls`, `cat`, `grep`, `git status/diff/log`,
  `pytest --collect-only`, and similar prefixes.
- **Any shell metacharacter forces approval**, even after an allowed prefix. `cat a > b` writes a
  file and looks like `cat`. This is the rule that matters most.
- **A prefix cannot express "unless a later flag"**, so destructive flags live in a rule's
  `deny_tokens`: `find -delete`, `git branch -D`, `git show --output=file`.
- **The dangerous-token list is short on purpose.** Adding `-r` or `-f` would send `grep -r` to the
  user, and a gate that cries wolf gets bypassed.
- **`stdin` is closed.** An interactive command hangs the run; failing fast is the honest behaviour.
- **No checkpoint is possible for a shell command**, and the approval prompt says so — we cannot know
  which files it will touch. Git is the safety net for that class of change.
- **No in-process sandbox**, matching pi's stated reasoning: partial isolation gets mistaken for a
  boundary while still depending on the host shell and credentials.

### Network access

`web_fetch` changes nothing locally and still needs a gate, so the approval chain is keyed on
**gated** tools rather than mutating ones (`aiden/tools/__init__.py:GATED_TOOLS`). Keying it on
"mutating" meant a fetch would have run with no gate at all.

- **Every fetch asks**, unless the host is in `AIDEN_FETCH_ALLOW`. A URL is opaque, so nothing about
  it is provable — and prompt fatigue is the failure mode, which is what the allowlist is for.
- **A query string can carry data out.** The prompt shows the URL in full for exactly that reason.
- **The resolved address is checked, not just the hostname.** A name that resolves to `127.0.0.1`
  defeats a string check; loopback, link-local, private, reserved, and the cloud metadata endpoint
  are all refused *before* any request is made.
- **Only text content types**, and reads stop at 2 MB with the stop reported.
- **A session cache exists** because re-fetching a page the run already read is the most expensive
  waste a tool can commit.

## Evaluation

`aiden eval` mines tasks from this repo, runs the agent against them in disposable git worktrees, and
gates a harness change. The rules that make its numbers mean anything:

- **A task is only adopted when it discriminates** — the tests must fail without the code and pass with
  it. Roughly 6 candidates in 46 survive this, and the rejections are the feature.
- **`resolved` needs both directions**: every fail-to-pass passes *and* every pass-to-pass still
  passes. A patch that fixes the target and breaks a neighbour is not a fix.
- **Infrastructure failures are not model failures.** A run that could not be set up is excluded from
  the denominator, so a broken runner cannot look like a weak model.
- **`< 3 pp` is noise**, and the gate checks cost as well as resolved rate. A harness that resolves
  more by spending ten times as much has not improved.
- **The set size is printed next to every rate.** Six tasks is an indication, not a result, and the
  report says so.
- **Eval sessions are written to `~/.aiden/eval-sessions/`**, not the user's session directory, so a
  benchmark does not fill their history with throwaway worktrees.

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
