# Reference Source Study: Pi 0.85.1 and Prime Agent (RLM + Continual Harness)

Primary-source teardown of the two harnesses Aiden is modelled on. Everything below was read
out of installed/checkouted source, not blog summaries:

- **Pi** `@earendil-works/pi-coding-agent@0.85.1` — installed at
  `~/.nvm/versions/node/v24.12.0/lib/node_modules/@earendil-works/pi-coding-agent`, plus its
  workspace deps `pi-agent-core`, `pi-ai`, `pi-tui`, `chord`. ~51k LOC of JS in
  `pi-coding-agent/dist` alone.
- **Prime Agent** `github.com/PrimeIntellect-ai/prime-agent` — monorepo
  (`packages/agent`, `packages/ai`, `packages/coding-agent`, `packages/tui`,
  `prime-agent-runtime/` Python). This is the reference implementation of **RLM** +
  **Continual Harness**.

## TL;DR

- Pi's loop is ~560 lines and shockingly small: a nested `while` with three injectable async
  hooks (`getSteeringMessages`, `prepareNextTurn`, `shouldStopAfterTurn`). That is the whole
  "agent". Everything else is context assembly and tool plumbing. [1]
- Pi's design bet: **the transcript tree is the source of truth**, everything (TUI, LLM
  request, compaction, fork, resume) is a projection of it. `buildContextEntries()` walks
  leaf→root; `buildSessionContext()` turns that path into messages. [2]
- Tool output budgets are hard constants, not vibes: **2000 lines / 50 KB / 500 chars per grep
  line**. Truncation always tells the model *how to continue* (`offset=N`). [3]
- An assistant message with `stopReason === "length"` **fails every tool call in it** rather
  than executing possibly-truncated arguments. Cheap, obviously-correct guard we must copy. [1]
- Tool batches default to parallel execution; a single tool marked
  `executionMode: "sequential"` forces the whole batch sequential. A batch where *every* result
  sets `terminate: true` ends the run. [1]
- Prime Agent's RLM is **not** "more tools": it collapses the built-in model tool surface to
  *one* tool — a persistent Python kernel — and makes subagents, files, shell, skills, and even
  context management function calls inside it. [4]
- Prime Agent's Continual Harness is the missing piece in every other harness: an append-only
  ledger of 4 entry kinds (`prompt | memory | skill | subagent`) that a separate reviewer model
  edits with **small, evidence-backed, snapshot-and-rollback-able** create/update/delete diffs.
  The base system prompt is *immutable by design*. [5]
- Two-process split (TS host owns credentials/transcript/scheduling; Python kernel owns working
  state) is what makes "self-update" survivable. The kernel is explicitly **not a security
  sandbox**. [4][6]
- For Aiden: build Pi-shaped core first (transcript tree, budgeted tools, hooks), then bolt on
  a Continual-Harness ledger, then — only if needed — a kernel-as-tool RLM layer.

## 1. The Pi loop, exactly

`pi-agent-core/dist/agent-loop.js` → `runLoop()`. Annotated control flow:

```
outer: while (true) {
  inner: while (hasMoreToolCalls || pendingMessages.length) {
    prepareNextTurn(lastCompletedTurn)?   // may be SLOW: compaction, model swap
    re-poll steering messages if compaction ran
    inject pendingMessages into context   // user "steering" mid-run
    message = streamAssistantResponse()   // transformContext → convertToLlm → stream
    if (stopReason in {error, aborted}) → turn_end, agent_end, return
    toolCalls = filter(content, type=="toolCall")
    hasMoreToolCalls = false
    if (toolCalls.length) {
      batch = (stopReason=="length")
        ? failToolCallsFromTruncatedMessage(toolCalls)   // ← guard
        : executeToolCalls(...)
      hasMoreToolCalls = !batch.terminate
      append each toolResult to context
    }
    turn_end
    if (shouldStopAfterTurn(turn)) → agent_end, return
    pendingMessages = getSteeringMessages()
  }
  followUps = getFollowUpMessages()       // queued after the agent decided to stop
  if (followUps.length) { pendingMessages = followUps; continue }
  break
}
```

Three things worth stealing wholesale:

| Mechanism | Why it matters |
|---|---|
| `steering` vs `followUp` queues | Two different injection points: *interrupt the run* vs *resume after it would have ended*. Most harnesses conflate them. |
| `prepareNextTurn` returns a **snapshot** (`context`, `model`, `thinkingLevel`) | Compaction and model switching are expressed as "the next turn may run against a different context", not as mutation of a live object. |
| re-poll steering *after* `prepareNextTurn` | Compaction is slow; the user may type during it. Without this, input is dropped. A one-line fix for a real bug. |

`streamAssistantResponse` also shows the two-stage message model:
`AgentMessage[]` (harness-level, extensible) → `transformContext` → `convertToLlm` →
`Message[]` (provider-level). Custom entry types never leak into provider payloads.

### Tool execution pipeline

`prepareToolCall` → `executePreparedToolCall` → `finalizeExecutedToolCall` → `tool_execution_end`
→ tool result message. Notable:

- Unknown tool name → immediate **error result**, not an exception. The model sees it and
  self-corrects. Same for schema-invalid args (`validateToolArguments`) and for
  `beforeToolCall` returning `{ block: true, reason }` — the *permission system is a boolean
  hook on the same path*, and `block` can also carry `terminate`.
- Parallel path builds a list of thunks and `Promise.all`s them, then **emits result messages in
  the original tool-call order** — nondeterministic completion, deterministic transcript. That
  is the entire trick for cache-friendly parallel tools.
- `prepareArguments` lets a tool repair its own args before validation (streaming-JSON
  salvage lives here).

## 2. Tool surface and its budgets

`pi-coding-agent/dist/core/tools/truncate.js` exports the constants every tool shares: [3]

```js
export const DEFAULT_MAX_LINES = 2000;
export const DEFAULT_MAX_BYTES = 50 * 1024;               // 50KB
export const GREP_MAX_LINE_LENGTH = 500;                  // per matched line
```

| Tool | Budget behaviour (from source) |
|---|---|
| `read` | `truncateHead` at 2000 lines / 50KB, whichever first; `offset`/`limit` page through; out-of-range offset is an error; a *single line* bigger than 50 KB returns a message telling the model the exact `sed -n 'Np' file \| head -c 51200` to run instead |
| `grep` | ripgrep via `ensureTool("rg")` (auto-download if absent), `.gitignore` respected, caps at `DEFAULT_LIMIT` matches or 50 KB, each match line clipped to 500 chars with a note "use read to see full lines" |
| `bash` | streaming output accumulator + `truncateTail` (keeps the *end* — the part that matters for build/test logs) |
| `edit` | exact-string `edits[]` array, matched against the **original** file, rejects overlapping/nested edits, `oldText` must be unique |
| `find`/`ls` | own caps |

Design reading: **every truncation is paired with a continuation instruction.** A bare "…
truncated" leaves the model guessing; `use offset=2001 to continue` turns it into one cheap
follow-up call. Budget *per tool call*, never per session.

`edit`'s "matched against the original file, all edits in one call" contract is what lets the
tool reject overlapping edits up front instead of corrupting a file mid-loop.

## 3. Transcript tree, compaction, branch summaries

Session file = append-only JSONL, `parentId` on every entry → **tree**, one leaf = current
position. Entry types include `message`, `model_change`, `thinking_level_change`,
`compaction`, `branch_summary`, `custom`, `custom_message`, `label`, `session_info`. [2]

Context building is a pure function of (path to leaf, latest compaction):

```
buildContextEntries(): walk leaf→root; if a CompactionEntry is on the path:
    emit compaction first
    if retainedTail: treat as self-contained checkpoint, take entries after compaction
    else: take firstKeptEntryId→compaction, then everything after compaction
```

Auto-compaction predicate and defaults: [7]

```
trigger when  contextTokens > contextWindow - reserveTokens      # reserveTokens = 16384
cut point    = walk back from newest, accumulate until keepRecentTokens (default 20k)
summary      = LLM over [previousBoundary .. cutPoint], previous summary passed as iterative context
entry        = CompactionEntry{ summary, firstKeptEntryId, retainedTail? }
```

Two subtleties that are easy to get wrong:
- The check runs **inside** a run — after tool results are appended, before the next assistant
  request — not just between turns. Long runs are where context dies.
- `retainedTail` makes a newer compaction a *self-contained checkpoint* (a handoff), while
  `firstKeptEntryId`-only compactions stay incremental for backwards compatibility.
- Compaction is also hookable: extensions implement `session_before_compact` /
  `session_compact_failed` and can supply their own summary. Branch summarization
  (`branch_summary`) covers the *other* direction — summarizing what you abandoned when you
  fork, with cumulative file tracking.

## 4. Extensibility: extensions, skills, templates

- **Extensions** are TS modules with an exported factory receiving `ExtensionContext`;
  registered against ~40 lifecycle events (`session_*`, `agent_*`, `turn_*`, `message_*`,
  `tool_execution_*`, `model_*`, `input_*`, `resource_*`, `user_bash_*`), plus
  `ctx.registerTool()`, `ctx.ui`, slash commands, `ctx.compact()`, `ctx.fork()`,
  `ctx.navigateTree()`, `ctx.switchSession()`. The UI is scriptable from the same surface —
  this is how you get custom footers/overlays without forking the TUI. [8]
- **Skills** = `SKILL.md` with frontmatter; **only name+description go into the system prompt**
  as XML; the model `read`s the body when the task matches; `/skill:name args` forces loading.
  Progressive disclosure implemented as *a directory and one prompt line*. [9]
- Project-trust gating exists (`ctx.isProjectTrusted()`, `trust-manager.ts`,
  `project-trust.ts`) — relevant to Aiden's self-update safety story.

## 5. Prime Agent: the RLM contract

Verdict from the docs/source: RLM = **"context is a variable in a persistent REPL, and
subagents are function calls"**. [4]

```python
# everything starts here — the built-in model tool is literally one tool: `ipython`
config_files = list(Path(".").rglob("*.toml"))  # Python state survives turns + compaction
result = await bash("npm run check")  # project commands, own process each call
checks = bash("npm test")  # unawaited → background handle; a
# "Background command finished" notice is
# steered in at the next safe boundary
handle = await rlm.spawn("Review the auth flow", name="auth-reviewer")  # admission only!
children = await rlm.list_subagents()  # registry survives compaction/restart
await agent_message.send("check the new test", receiver_role="child", receiver_name=api_review.name)
report = await release_audit(repository=".", target_version="0.4.0")  # Python-backed skill
```

Non-obvious invariants worth copying exactly:

1. **`rlm.spawn` never returns the child's answer.** It returns
   `RLMSpawnHandle{rlm_child_id, name, session_dir, model}` at admission time. Answers arrive
   *later* as ordinary `agent_message`s or as files. This single rule kills the
   "block-and-hope-the-parent-still-fits-in-context" failure mode and makes fan-out free.
2. **Depth is a counter in the environment** (`RLM_DEPTH < RLM_MAX_DEPTH`, default max 2), and
   unknown `rlm.spawn` options **fail loudly** instead of being ignored. Model selection is
   strict: no silent fallback to another model.
3. **Child cost is attributed to the parent assistant *turn*** via a persisted
   `child_usage_attributed` transcript entry; context-tree reporting *subtracts* it so "own
   context" and "billable total" stay reconcilable. Any multi-agent harness that can't answer
   "what did that subagent cost the parent turn" is broken.
4. **Host/kernel split.** JSON-lines over stdio; request set
   `execute, interrupt, host_reply, snapshot, restore, list_names, shutdown`; event set
   `ready, stdout, stderr, result, display, host_request, error, done`. Python never talks to a
   provider and never writes the transcript: credentials, provider calls, transcript writes,
   worker routing, scheduling stay behind typed `host_request`s. One kernel serializes ordinary
   cells (single shared namespace) but children run concurrently on their own runtimes.
   Kernel namespace can be **snapshotted to `kernel-state.dill`** and revived.
5. **Answer via mutable variable** (in the verifiers/env flavour):
   `answer = {"content": "", "ready": False}`, REPL stdout clipped to 8192 chars/turn —
   the model is *forced* into programmatic processing, and finalization becomes iterative.
   `llm_batch()` fans out parallel sub-LLM calls. [6]
6. **Trust model stated plainly:** kernel runs model-generated Python with the worker's OS
   permissions; process boundaries are for lifecycle/failure containment, **not** security. [6]

Measured RLM behaviour (GPT-5-mini, Prime Intellect ablations) — worth reading before we
commit to a kernel-first design: [6]

| Env | Effect of RLM |
|---|---|
| DeepDive (deep research, tool-heavy) | needs explicit `<env_tips>` strategy to beat plain LLM; big main-context compression |
| Oolong (real long-context) | clearly better than plain LLM at ~1.5M chars input; worse on synthetic subsets |
| math-python | **worse** than plain Python-tool LLM — scaffolding cost, suspected benchmark overfit |
| verbatim-copy | better, but very different (multi-turn, tool-heavy) strategy |

i.e. RLM is a *context-folding* win on long-context/tool-token-heavy tasks and a *latency and
sometimes accuracy tax* on short, already-scaffolded tasks. Timing got worse everywhere.

## 6. Continual Harness: the ledger, in code

`prime-agent-runtime/src/rlm/harness.py` — `HarnessState`, "CRUD store for reset-free harness
refinement state": [5]

```python
HarnessKind  = Literal["prompt", "memory", "skill", "subagent"]
HarnessScope = Literal["local", "global"]

@dataclass
class HarnessEntry:            # versioned, timestamped, grouped
    id, kind, title, content: str
    path: str = "general"      # grouping/scope-ish
    scope: HarnessScope = "local"
    reference: dict = {}       # skills MUST be {"type":"python", import, callable|call_pattern}
    arguments: dict = {}       # declared inputs/required/defaults
    metadata: dict = {}        # e.g. {"scope":"local"} = intended blast radius
    source: str = "agent"
    version: int = 1           # bumped on update

@dataclass
class RefinementEvent:         # one review pass, append-only
    id: str                    # refine_0001
    trigger: str
    changes: list[str]
    evidence: str              # ← required-ish: what in the trajectory justifies this
    outcome: str               # expected outcome, for later validation
```

Storage: single JSON file. Session-local at
`<session-artifacts>/harness/harness_state.json`; global at `~/.prime/agent/harness/`.
**`_sync_from_disk()` on mtime before every mutation** so host-side `/refine` writes and
kernel writes don't clobber each other — the cheapest possible fix for two writers on one
learning state. Retrieval is deliberately boring: weighted term-overlap scoring across
title/content/path+id with recency tie-break, `overview()` renders a bounded digest
(`max_entries_per_kind=20`, 120-char content previews, `+N more`) — i.e. **the prompt-visible
projection of learned state is size-capped by construction.** `snapshot()` returns the whole
ledger for rollback; `plan_refinement()` returns a 3-step "diagnose → smallest edit → validate
and record outcome" plan.

`/refine` (`packages/coding-agent/src/core/refinement/refinement.ts`) is a *separate model
call with its own system prompt* whose output is strict JSON: [5]

```json
{ "summary": "...", "rationale": "why these edits are justified by trajectory evidence",
  "expectedOutcome": "what should improve and how to validate it",
  "edits": [{ "action": "create|update|delete", "kind": "prompt|memory|skill|subagent",
              "id": "...", "title": "...", "content": "...", "path": "...",
              "reference": {...}, "arguments": {...}, "metadata": {}, "reason": "..." }] }
```

Rules encoded in that prompt, which are the actual safety design:

- "This is similar in spirit to context compaction, but instead of summarizing the conversation
  you emit precise Create/Update/Delete edits to reusable state."
- **`prompt` = supplemental notes only; "The base system prompt is immutable and MUST NOT be
  rewritten."**
- Default store is **local to the session**; global writes need an explicit request and must be
  "stable cross-session lessons" — project-specific lessons may only go global when the
  title/path/content *names the project*.
- During a local refinement, global entries are **read-only context** — never propose
  update/delete on them; create a local override instead.
- Routing heuristic: repeated delegation role → `subagent`; repeated procedure → `skill`;
  durable fact/preference → `memory`; narrow behavioural policy → `prompt`.
- "Prefer small evidence-backed edits… If prior refinements caused issues, rollback or replace
  the faulty entries. **Never edit source files directly.**"
- `applyRefinementProposal` validates edit fields **at apply time** (the proposal is treated as
  untrusted input; invalid fields are preserved for error reporting), guards against an edit
  racing a concurrently-modified entry, and writes a `refinements.jsonl` history;
  `rollbackProposal(target)` synthesizes the inverse edit set from recorded before/after
  snapshots. [5]
- **Auto-refine is gated by a second cheap model call** (`shouldRefine`, `rationale`,
  `instructions`) that explicitly rejects "one-off noise, unsupported hypotheses, transient
  tool outputs". Two-model design: a *judge* (should we learn?) and a *writer* (what to learn).
- Output-token caps: refinement 32k, review gate 4k, +1k context overhead; `maxTokens` is
  derived from the model's context window so the reviewer itself can't overflow.
- `refine` is exposed *as a skill* callable from the kernel (`await refine.run()`), so the
  agent can decide to learn — and the trigger list is injected into its prompt: "after a
  repeated failure, a reusable tactic emerges, a repeated delegation role should become a
  subagent spec… a user corrects behavior that should persist… validation shows an entry is
  wrong".

## 7. What Aiden adopts (decisions, not vibes)

| # | Adopt | From | Adaptation for Aiden |
|---|---|---|---|
| 1 | append-only entry **tree** JSONL as the single source of truth | Pi | same, plus `harness_edit` entry type so learning actions are first-class transcript nodes |
| 2 | budgeted tools with continuation hints (2000/50KB style) | Pi | keep numbers, add per-turn output budget across a tool batch |
| 3 | `stopReason=="length"` → fail all tool calls | Pi | copy verbatim |
| 4 | parallel tool exec, transcript-order result emit | Pi | copy verbatim |
| 5 | steering vs follow-up queues; re-poll after slow `prepareNextTurn` | Pi | copy verbatim |
| 6 | compaction inside a run, `reserveTokens`/`keepRecentTokens` defaults | Pi | start at 16384/20k; make the summariser pluggable |
| 7 | skills = metadata in prompt, body on demand | Pi/Agent Skills | identical; Aiden also allows *learned* skills |
| 8 | 4-kind versioned harness ledger + `refine_####` events + snapshots | Prime | **the core of Aiden's continual learning** |
| 9 | two-model refine (gate + writer), immutable base prompt, local-by-default, apply-time validation, rollback | Prime | add an *eval gate* before a global write (Prime does not have one) |
| 10 | one persistent kernel as the model's main surface | Prime RLM | **defer to v2**; v1 keeps Pi-style discrete tools (see RLM table above: it's a tax on short tasks) |
| 11 | `spawn`-returns-handle-at-admission; answers as messages | Prime | adopt when we add subagents, from day one, so the API never has to change |
| 12 | child-usage attribution into the parent turn + subtract-in-tree reporting | Prime | adopt with subagents |
| 13 | host owns creds/transcript/schedule; runtime is a thin shim | Prime | adopt as an architectural rule even without a Python kernel |
| 14 | "kernel is not a sandbox" honesty + project-trust check | both | Aiden adds a real boundary before self-modification (see architecture doc) |

Explicitly **skipped**: daemon/reattach, heartbeats/cron, `--autonomous` budgets, ACP, MCP
catalog, standalone binary packaging. Those are operational scale features; they don't teach
the mechanisms and they'd swallow the build.

## Open questions

- `retainedTail` provenance (who writes it, when) — needs a pass through
  `core/compaction/*` in the pi source; docs describe it only as "optional".
- How Prime Agent validates a refinement's `expectedOutcome` afterwards. The ledger stores it,
  but I found no automatic re-check; the paper's co-learning loop is offline RL, not runtime
  validation. If there is none, that's exactly the gap Aiden should fill with a private eval set.
- Pi's `transformContext` looks like the general-purpose slot for programmatic context folding
  (the RLM idea without a kernel). Worth prototyping there.
- Whether Prime's default `RLM_MAX_DEPTH = 2` was tuned empirically or just set.

## Sources

- [1] `pi-agent-core@0.85.1` `dist/agent-loop.js` — `runLoop`, `streamAssistantResponse`,
  `executeToolCalls*`, `prepareToolCall`, `failToolCallsFromTruncatedMessage`,
  `shouldTerminateToolBatch`
- [2] pi `docs/session-format.md` — entry types, tree structure, `buildContextEntries()`,
  `buildSessionContext()`
- [3] pi `dist/core/tools/truncate.js`, `read.js`, `grep.js`, `edit.js`, `bash.js`
- [4] prime-agent `packages/coding-agent/docs/rlm.md` — RLM programming model, core invariants
- [5] prime-agent `prime-agent-runtime/src/rlm/harness.py`;
  `packages/coding-agent/src/core/refinement/refinement.ts` (`REFINEMENT_SYSTEM_PROMPT`,
  `AUTO_REFINE_REVIEW_SYSTEM_PROMPT`, `applyRefinementProposal`, `rollbackProposal`,
  `refinements.jsonl`); `packages/coding-agent/docs/rlm-runtime.md` §"Continual Harness State"
- [6] Prime Intellect, *Recursive Language Models: the paradigm of 2026*,
  https://www.primeintellect.ai/blog/rlm ; paper https://arxiv.org/abs/2512.24601 ;
  original blog https://alexzhang13.github.io/blog/2025/rlm/ ;
  impl https://github.com/alexzhang13/rlm ; env-side RLM in
  https://github.com/PrimeIntellect-ai/verifiers (branch `sebastian/experiment/rlm`)
- [7] pi `docs/compaction.md` — trigger formula, cut point rules, `CompactionEntry`, defaults
- [8] pi `docs/extensions.md` — lifecycle events, `ExtensionContext`, `registerTool`,
  `ctx.compact`, `ctx.fork`
- [9] pi `docs/skills.md`; Agent Skills spec https://agentskills.io
- [10] prime-agent README — "A Self-Improving RLM Harness", Continual Harness paper
  https://arxiv.org/abs/2605.09998 ; install/CLI surface https://app.primeintellect.ai/prime-agent/install.sh
- [11] prime-agent `packages/coding-agent/docs/architecture.md` — client/supervisor/worker/
  kernel/storage boundaries; `rlm-runtime.md` §Stdio Transport, §Child Execution,
  §Usage and Cost Attribution, §Failure Modes
- [12] Prime Intellect *Continual Harness* abstract (arXiv:2605.09998): reset-free online
  refinement of prompt/subagents/skills/memory, and the online process-reward co-learning loop
