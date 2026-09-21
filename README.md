# Project Aiden

A personal, **learning-oriented** agentic harness for coding + research. The agent inside it
is called Aiden.

This is not an attempt to beat Claude Code / Pi / OpenClaw / Aider. The point is to
understand the mechanisms deeply enough to rebuild them, and to try two things the
mainstream harnesses do not:

1. **Continual learning** — the harness gets better the more you use it.
2. **Self-update** — the agent proposes and applies patches to its own tooling, prompts and
   skills, gated by an eval rather than an apology.

## Status

| Piece | State |
|---|---|
| Research corpus (`docs/research/`, 10 docs) | done |
| Architecture + ADRs (`docs/architecture/`) | done |
| **Provider suite** (`aiden/providers/`) | **done — 42 providers, 3 wire protocols, live-verified** |
| v0.1 minimal harness (read code, answer questions) | in progress — see `docs/plan/v0.1-minimal-harness.md` |
| TUI (first product deliverable) | not started — see `docs/research/06-ui-ux.md` |

## Quick start

```bash
uv sync
uv run python -m pytest -q          # 86 tests, no network

# credentials: reuse what pi already has, or set env vars
uv run aiden providers import pi
uv run aiden providers check

# provider catalogue
uv run aiden providers list
uv run aiden providers show opencode-go
uv run aiden providers models muse-spark
```

Live checks (spend money, need network):

```bash
uv run python scripts/smoke_providers.py   # one call per provider
uv run python scripts/smoke_tools.py       # full tool round-trip per provider
```

## Configuration

Every constant lives in `aiden/config.py` with the source that justifies it. Environment
overrides:

| Variable | Default | Meaning |
|---|---|---|
| `AIDEN_HOME` | `~/.aiden` | credential + session root |
| `AIDEN_MODEL` | `opencode-go/qwen3.8-flash` | default model (`provider/model[:level]`) |
| `AIDEN_MAX_TOKENS` | `4096` | per-response output cap |
| `AIDEN_THINKING` | `off` | thinking level |
| `AIDEN_MAX_TURNS` | `10` | loop turn ceiling |
| `AIDEN_MAX_COST_USD` | `0.50` | hard per-run spend ceiling |
| `AIDEN_AUTH_FILE` | `~/.aiden/auth.json` | credential file |
| `AIDEN_PROVIDERS_FILE` | `providers.toml` | catalog overrides |

Sessions live in `~/.aiden/sessions/--<project-path>--/` — deliberately outside the repo so
transcripts can never be committed. `providers.toml` is gitignored because it can hold keys.

## Layout

```
aiden/
  config.py      all constants, each with a source comment
  retry.py       error kind -> loop action (retry | compact | surface | abort)
  providers/     L1 transport: IR, catalog, auth, 3 wire protocols (+ quirk layer)
  cli/           `aiden` command line
scripts/         catalog sync + live smoke tests
tests/           fixture-driven, no network
docs/
  BRIEF.md       the research brief all six research agents worked from
  research/      evidence layer (10 docs, every claim sourced)
  architecture/  decision layer (system design + ADRs)
  plan/          v0.1 harness plan, provider suite design
  diagrams/      mermaid diagrams
```

## Design rules

- **Evidence, then decisions.** `docs/research/` is the evidence layer; `docs/architecture/`
  is the decision layer and cites it. Numbers are consolidated in
  `docs/research/09-measured-evidence.md`.
- **Every constant is justified.** If a number appears in code without a source, that is a bug.
- **The session log is the product.** The TUI, the LLM request, resume, fork and the
  refiner's view are all projections of the same append-only entry tree.
- **Errors are values.** Provider and tool failures return messages the model can see and
  correct; the loop never dies from a bad tool call.
- **Caps leave traces.** Truncation, budget exhaustion and dropped tool calls are reported
  explicitly, never silently.

See `AGENTS.md` for contributor/agent conventions.
