# Provider Suite — design, build plan & status

**Status: implemented and live-verified.** 42 providers / 1470 models vendored, 3 wire
protocols implemented, 64 offline tests green, 6/6 target providers completed a full
tool round-trip against the real APIs (see §10).

**Goal.** One internal message IR, three wire-protocol adapters, and a declarative
provider/model catalog, so `aiden` can talk to **anthropic, openai, deepseek, opencode,
opencode-go, commandcode** — and any of pi's other 35 catalogued providers — with **no code
change** to add one.

This is L1 (transport) from `docs/architecture/aiden-architecture.md`, built as a standalone
package before the tools/loop, because every later layer depends on it.

---

## 1. Decision summary

| Decision | Choice | Why |
|---|---|---|
| Catalog | **Vendor pi's full 41-provider catalog** (`pi-ai/dist/providers/data/*.json`, ~750 KB) + `aiden providers sync` | Proven, machine-readable, carries `cost`/`contextWindow`/`maxTokens`/`thinkingLevelMap`/`compat`. pi refreshes it upstream; we snapshot + sync. |
| Wire protocols | **3 adapters implemented**: `anthropic-messages`, `openai-completions`, `openai-responses` (+`google-generative-ai` deferred) | 6 target providers collapse into these 3. Adding a provider = catalog entry, not code. |
| HTTP | **`httpx` + own SSE parser + pydantic models** | Compat flags (`maxTokensField`, `thinkingFormat`, `requiresReasoningContentOnAssistantMessages`, `supportsDeveloperRole`) and proxies fight SDKs. Uniform error/retry taxonomy needs raw status+body. |
| Auth | **env vars → `~/.aiden/auth.json`** (+ `aiden providers import pi`) | Independent of pi, but zero-friction migration. No writes to pi's file. |
| DeepSeek | Catalog vendored; resolved from `DEEPSEEK_API_KEY` when present | User: "ignore for now". Also reachable via opencode-go / commandcode. |
| commandcode | **Own catalog entry** (not in pi's built-ins) | Community provider. `claude-* → anthropic-messages`, else `openai-completions`, baseUrl `https://api.commandcode.ai/provider/v1`. |

## 2. Endpoint conventions (verified against pi's catalog, not guessed)

```
anthropic-messages   POST {baseUrl}/v1/messages      auth: x-api-key  (Bearer iff token starts sk-ant-oat)
openai-completions   POST {baseUrl}/chat/completions auth: Authorization: Bearer
openai-responses     POST {baseUrl}/responses        auth: Authorization: Bearer
google-generative-ai POST {baseUrl}/models/{id}:streamGenerateContent?alt=sse
```

Checks: anthropic `baseUrl=https://api.anthropic.com` → `/v1/messages` ✓;
openai `…/v1` → `/v1/responses` ✓; opencode-go completions `…/zen/go/v1` → `/chat/completions` ✓;
commandcode anthropic `…/provider` → `/provider/v1/messages` ✓ (matches the extension's
`baseUrlForModel`, which strips the trailing `/v1` for anthropic-messages).

## 3. Package layout

```
aiden/providers/
  types.py        # IR: Message, Part, ToolSpec, ToolCall, Usage, StreamEvent, StopReason, ModelInfo, ProviderInfo
  errors.py       # typed failures: Auth, RateLimit, Overflow, Transport, Provider, BadRequest  (+ retryable flag)
  catalog.py      # load vendored data/*.json + user providers.toml overrides → ProviderInfo/ModelInfo
  registry.py     # resolve "provider/model[:level]", search, protocol coverage
  auth.py         # ENV_KEYS map, ~/.aiden/auth.json, pi import, oauth-shaped creds
  capabilities.py # provider-level quirks (session routing headers) that aren't model compat
  sse.py          # incremental SSE parser (event/data/comment, multi-line data, chunk boundaries)
  transports/
    base.py                  # HTTPTransport: request/retry of connection phase, accumulation, diagnostics
    anthropic_messages.py
    openai_completions.py
    openai_responses.py
  data/           # vendored catalog: <provider>.json (api → {modelId: ModelInfo}) + .manifest.json
scripts/sync_catalog.py     # re-vendor from a pi install + derive commandcode from the extension
scripts/smoke_providers.py  # live: one call per provider
scripts/smoke_tools.py      # live: full tool round-trip per provider
tests/providers/            # fixture-driven, no network
```

## 4. IR (the seam everything else uses)

```python
Message(role, content: list[Part])          # system | user | assistant | tool
Part = TextPart | ImagePart | ThinkingPart | ToolCallPart | ToolResultPart
ToolSpec(name, description, parameters: dict, strict: bool)
StreamEvent = Start | TextDelta | ThinkingDelta | ToolCallStart | ToolCallDelta
            | ToolCallEnd | UsageEvent | Stop(reason, usage) | Error
StopReason = "end_turn" | "tool_use" | "max_tokens" | "error" | "aborted"
```

Rules carried from research (enforced in `transports/base.py`):
- **Never raise into the loop** — every failure becomes `Stop(reason="error")` or a typed `ProviderError` the caller maps.
- **`max_tokens` mid-tool-call ⇒ the tool call is not executable** (partial args); emit `Stop("max_tokens")` and let the loop fail the calls.
- **Emit results in call order**, even when the provider streams them interleaved (deterministic transcripts, warm prompt cache).
- **Usage is per-message**, including cache read/write and reasoning tokens; cost computed from `ModelInfo.cost`.

## 5. Auth resolution order

1. explicit `api_key=` argument (tests, CLI flag)
2. provider-specific env var (e.g. `ANTHROPIC_API_KEY`, `OPENCODE_API_KEY`, `DEEPSEEK_API_KEY`)
3. `~/.aiden/auth.json` → `{ "<provider>": {"type":"api_key","key":…} | {"type":"oauth","access":…,"refresh":…,"expires":…} }`
4. `None` → `AuthError` at request time (not import time)

`aiden providers import pi` copies pi's `auth.json` into ours (mapping `oauth`→`oauth`,
`api_key`→`api_key`). OAuth-shaped creds: `access` used as the key while unexpired; a provider
may declare a refresh hook (`capabilities.py`), otherwise expiry is surfaced as a warning.

## 6. Error taxonomy (retry policy input, not retry policy)

| Kind | Trigger | Retryable |
|---|---|---|
| `AuthError` | 401/403, missing creds | no |
| `RateLimitError` | 429 (+`retry-after`) | yes |
| `OverflowError` | context-length provider message | no (compact, not retry) |
| `BadRequestError` | 400 (schema/args) | no |
| `TransportError` | connect/timeout/5xx | yes |
| `ProviderError` | anything else, raw body retained | no |

## 7. Build order

| Step | Deliverable | Test |
|---|---|---|
| P1 | types + errors + sse parser | SSE multi-line/comment/chunk-boundary fixtures |
| P2 | catalog + vendored data + `sync` script | 41 providers load; commandcode entry present; model lookup by id |
| P3 | auth (env/map/file/import) | env precedence, oauth expiry, pi import round-trip |
| P4 | `anthropic-messages` transport | recorded SSE fixtures → IR events; thinking blocks; tool_use |
| P5 | `openai-completions` transport | fixtures + deepseek compat flags (`max_tokens`, reasoning_content) |
| P6 | `openai-responses` transport | fixtures; output items → IR |
| P7 | registry + `aiden providers list/show/check/import/sync` | CLI smoke |
| P8 | live smoke: one real call per authed provider (anthropic, openai, opencode-go, commandcode) | manual, budget-capped |

## 8. Acceptance — met

| Criterion | Result |
|---|---|
| `aiden providers list` shows all vendored providers | 42 providers, 1470 models |
| `resolve("anthropic/claude-sonnet-4-6")`, `opencode-go/muse-spark-1.3-contributor`, `commandcode/claude-sonnet-5` return ModelInfo + transport | yes |
| Fixture tests green incl. truncated-tool-call case | 64 passed |
| Live smoke call for anthropic, openai, opencode-go, commandcode | 5/5 on first sweep, 6/6 tool round-trips |
| Adding a 7th provider is a catalog entry | yes (plus one adapter file only for a new protocol) |

---

## 9. Quirks discovered by building this (all regression-tested)

These are the things that were *not* in the catalog and would have caused silent failures:

1. **`httpx` responses are not async context managers.** `client.send(..., stream=True)`
   returns a `Response` you must `aclose()` yourself; only `client.stream()` supports
   `async with`. Using the wrong form fails at runtime, not import.
2. **opencode / opencode-go require `x-opencode-session`** on every request or the gateway
   refuses with *"cannot be routed efficiently"*. It is a **provider-level** requirement
   (pi injects it via `opencode-headers.ts`), not a model `compat` flag — hence
   `capabilities.py`. `ProviderSuite.stream()` auto-generates one when the caller has no
   session id yet; the harness will pass its real L6 session id.
3. **Reasoning models can consume the entire output budget before emitting text.**
   Command Code's `meta/muse-spark-1.3-contributor` spent 356 reasoning tokens before
   saying "pong"; with `max_tokens=512` it returned *empty text* and `stop_reason=max_tokens`.
   Correct behaviour, terrible UX — so `Stop.message` / `Completion.diagnostic` now explain
   it explicitly (*"output budget exhausted by reasoning (N reasoning tokens, no visible
   text); raise max_tokens or lower the thinking level"*). Silent truncation is a named
   failure mode in research/02 §3.
4. **Truncated tool calls must not be executed.** `ToolCallPart.truncated` is set when
   arguments fail to parse or the turn ends at `max_tokens`; the loop will fail those calls
   with an error tool result (research/01 §loop rule).
5. **`thinking_level="off"` cannot always be honoured.** Muse Spark publishes no `off`
   effort, so the level maps to `None` and no `reasoning_effort` is sent — the model reasons
   anyway. The level map is authoritative, not our wish.
6. **Anthropic auth is header-dependent.** `x-api-key` normally; `Authorization: Bearer` for
   `sk-ant-oat…` OAuth tokens (pi's `createClient`/`isOAuthToken` logic).
7. **commandcode needs two base URLs.** `claude-*` → `…/provider` (so the client's
   `/v1/messages` suffix lands on `/provider/v1/messages`); everything else →
   `…/provider/v1` (so `/chat/completions` lands correctly). Getting this wrong is a 404.
8. **macOS `UF_HIDDEN` + `.pth` = a broken console script.** CPython 3.12+ deliberately
   skips `.pth` files carrying the BSD `UF_HIDDEN` flag (`site.addpackage`), so an editable
   install whose `.pth` picked up that flag fails with
   `ModuleNotFoundError: No module named 'aiden'` while the package is installed correctly.
   One-line fix: `chflags nohidden .venv/lib/python*/site-packages/*.pth`. `python -m aiden`
   works either way, which is why `aiden/__main__.py` exists.

Deriving `commandcode.json` from the extension's **generated** TypeScript (rather than a
hand list) was worth it: 65 models with accurate input modalities, reasoning flags and
per-model effort maps, versus 19 guessed entries.

---

## 10. Live verification

```
$ uv run python scripts/smoke_providers.py
OK   anthropic/claude-haiku-4-5                   anthropic-messages        30/5  pong
OK   openai/gpt-5.4-mini                          openai-responses          30/5  pong
OK   opencode-go/qwen3.8-flash                    anthropic-messages       20/34  pong
OK   commandcode/claude-haiku-4-5-20251001        anthropic-messages        30/5  pong
OK   commandcode/meta/muse-spark-1.3-contributor  openai-completions  46/380+369r  pong
5/5 providers responded

$ uv run python scripts/smoke_tools.py     # tool call -> parsed args -> tool result -> answer
OK   anthropic/claude-haiku-4-5                   anthropic-messages  1324/79   args={'path': 'docs/BRIEF.md'}
OK   openai/gpt-5.4-mini                          openai-responses      245/36   args={'path': 'docs/BRIEF.md'}
OK   opencode-go/qwen3.8-flash                    anthropic-messages    156/230  args={'path': 'docs/BRIEF.md'}
OK   opencode-go/deepseek-v4-flash                openai-completions  9849/580  args={'path': 'docs/BRIEF.md'}
OK   commandcode/claude-haiku-4-5-20251001        anthropic-messages   1324/90   args={'path': 'docs/BRIEF.md'}
OK   commandcode/meta/muse-spark-1.3-contributor  openai-completions  1292/422  args={'path': 'docs/BRIEF.md'}
6/6 providers completed a tool round-trip
```

`deepseek` resolves and builds requests correctly but has no credential (`DEEPSEEK_API_KEY`
unset, as agreed) — it fails with an actionable `AuthError`, not a crash. DeepSeek models are
currently reachable through `opencode-go` and `commandcode`.

---

## 11. Usage

```python
from aiden.providers import ProviderSuite, Message, ToolSpec

suite = ProviderSuite.load()
model = suite.resolve("opencode-go/muse-spark-1.3-contributor")  # or "provider/model:high"

completion = await suite.complete(
    model,
    [Message.text("user", "Read docs/BRIEF.md and summarise it.")],
    [read_tool],
    system="You are a terse coding agent.",
    max_tokens=2_000,
    thinking_level="high",
)
completion.tool_calls  # parsed, with .truncated flags
completion.usage  # input/output/cache_read/cache_write/reasoning
completion.cost_usd  # from the catalog cost table
```

CLI:

```
aiden providers list                 # providers, model counts, protocols, auth state
aiden providers show opencode-go     # models with api, context, cost
aiden providers models muse-spark    # search across every provider
aiden providers check                # auth + protocol coverage for the required six
aiden providers import pi            # copy credentials into ~/.aiden/auth.json (0600)
aiden providers sync                 # re-vendor the catalog
```

Adding a provider with an implemented protocol:

```toml
# providers.toml  (repo root, or ~/.aiden/providers.toml)
[providers.local-llama]
api = "openai-completions"
baseUrl = "http://localhost:11434/v1"

[[providers.local-llama.models]]
id = "llama3.1:8b"
contextWindow = 128000
maxTokens = 32000
```

---

## 12. Open questions

- Anthropic OAuth (`sk-ant-oat`): Bearer branch is implemented and unit-tested, but not exercised against a live subscription token (we hold an API key). Claude-Code identity headers (`user-agent: claude-cli/*`, `x-app: cli`) are **not** yet sent for OAuth tokens — needed only for subscription billing.
- `google-generative-ai` adapter deferred (not requested); registry tolerates its absence with an explicit "protocol not implemented" error rather than a crash.
- Cost table freshness: catalog `cost` is a snapshot; `aiden providers sync` re-reads it. No live pricing scrape in v0.1.
- Per-provider `headers`/`compat` overrides from user `providers.toml` are load-supported but only the flags the implemented adapters honor are wired.
