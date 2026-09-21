# Aiden — System Architecture

Design blueprint for the Aiden harness. Every claim marked `[→ ref]` is sourced in
`docs/research/`; this document is the **decision layer**, not the evidence layer. Evidence lives in:

| If you want to know… | Read |
|---|---|
| why the layers are these ones, what each loop shape costs | [research/01-harness-anatomy.md](../research/01-harness-anatomy.md) |
| why these 7 tools, budgets, patch format, search | [research/02-tooling-efficiency.md](../research/02-tooling-efficiency.md) |
| memory/skills/self-update design + guardrails | [research/03-continual-learning.md](../research/03-continual-learning.md) |
| RLM / Prime Intellect / recursive context | [research/04-prime-intellect-rlm.md](../research/04-prime-intellect-rlm.md) |
| benchmarks, eval mechanics, local recipes | [research/05-benchmarks-evals.md](../research/05-benchmarks-evals.md) |
| the UI (first build target) | [research/06-ui-ux.md](../research/06-ui-ux.md), [design/aiden-ui-spec.md](../design/aiden-ui-spec.md) |
| line-by-line teardown of Pi + Prime Agent | [research/00-reference-source-study.md](../research/00-reference-source-study.md) |
| paper-level mechanism of continual learning | [research/07-continual-learning-papers.md](../research/07-continual-learning-papers.md) |
| Prime Agent tech report + its refinement incident | [research/08-prime-agent-tech-report.md](../research/08-prime-agent-tech-report.md) |
| **every number** used below | [research/09-measured-evidence.md](../research/09-measured-evidence.md) |

---

## 0. What Aiden is, and what it is not

**Aiden** is a local, single-user coding **+ research** agent harness in Python, built to make its
own mechanisms legible and to get measurably better with use.

Three properties define it against Pi / Claude Code / Prime Agent:

1. **Continual learning is a first-class subsystem**, not a memory plugin. Harness state —
   prompt notes, memories, skills, subagent specs — is typed, versioned, auditable, and *scored*.
2. **Self-update is gated by an eval, not by an apology.** A proposal to change Aiden's own
   prompts/tools/skills must pass a paired eval run before it can be applied. (Pi and Prime Agent
   both refine state without an empirical gate; we add one — see
   [research/07](../research/07-continual-learning-papers.md) §6.9.)
3. **Learning is measured on a cost axis.** Because a frontier-model capability floor is documented
   (refinement *hurts* small models [12]) and infrastructure noise alone is worth 6 pp on real
   benchmarks [14], every claim Aiden makes about itself is `resolved @ cost` over ≥3 seeds.

**Not** in scope for v1: daemon/reattach, multi-user, heartbeats/cron, MCP client catalogue,
computer-use, embeddings-by-default, a Python-kernel-only loop (see ADR-07), fine-tuning weights.

---

## 1. Layer map

Nine layers, L0–L8, plus L9 accounting as a cross-cutting service
([research/01](../research/01-harness-anatomy.md) §layers):

```
                     user · CLI · ACP-ish client · (later: desktop)
  ┌──────────────────────────────────────────────────────────────────────┐
L8│ PRESENTATION   aiden/tui (own core) · event reducer · diff/permission UI │
L7│ DELEGATION     spawn(name, task, ctx=isolated|fork) · registry · lanes│
L6│ PERSISTENCE    append-only entry tree (JSONL) · fork/resume/rewind     │
L5│ CONTEXT        estimator · ingest caps · spill-to-file · compaction    │
L4│ TOOLS          registry · pydantic validate · policy · execute (par)   │
L3│ LOOP           turn state machine · steering/follow-up · recovery      │
L2│ ASSEMBLY       system prompt (immutable) + harness state (mutable)     │
L1│ TRANSPORT      provider adapters · streaming · retry/backoff · usage   │
L0│ ENVIRONMENT    cwd · git · shell · fs · sandbox boundary · config      │
  ├──────────────────────────────────────────────────────────────────────┤
L9│ ACCOUNTING (cross-cutting): per-entry usage incl. cache read/write,    │
  │   per-tool cost, child-usage attribution, harness-state version        │
  └──────────────────────────────────────────────────────────────────────┘
        ▲                                        │
        │            ┌───────────────────────────┘
        │            ▼
   ┌────┴─────────────────────────────────────────────────────┐
   │ LEARNING SUBSYSTEM (Aiden's reason to exist)             │
   │  harness_state ledger · refine(gate→writer) · probes     │
   │  consolidation (idle) · self-update proposals · evals    │
   └──────────────────────────────────────────────────────────┘
```

Rules:

- Each layer is swappable behind a typed contract; the test of layering is "can I replace Textual
  with a desktop renderer without touching L3?" (see ADR-09).
- **L2 is where the split between immutable and learned state lives**: `base_prompt` is
  version-controlled and *never* writable by the agent; everything the agent learns goes into
  supplemental state assembled after it ([research/03](../research/03-continual-learning.md),
  Prime Agent's immutable base prompt rule [11]).
- The learning subsystem is **not** a layer; it is an external loop that reads transcripts (L6) and
  writes L2 state + skills + subagent specs, and it must be observable from L8.

### Package layout

```
aiden/
  __init__.py
  config.py            # ALL constants, each with a source comment (§7)
  env/                 # L0
    fs.py  git.py  shell.py  trust.py
  transport/           # L1
    base.py            # StreamEvent, StopReason, Usage  (never raises)
    retry.py           # error taxonomy → policy (§5)
    providers/         # anthropic.py openai.py ollama.py commandcode.py
  assemble/            # L2
    system_prompt.py   # immutable base + sections
    harness_context.py # ledger → bounded prompt projection  (§4)
    skills.py          # SKILL.md discovery, metadata-only injection
    cache_layout.py    # ordering so only the last chunk busts the prompt cache
  loop/                # L3
    loop.py            # run_loop  (§2)
    turn.py            # frozen TurnState, atomic replacement
    queues.py          # steering vs follow-up
  tools/               # L4
    registry.py types.py    # Tool meta: read_only, concurrency_safe, budget
    validate.py policy.py   # schema → semantics → permission
    read.py grep.py glob.py edit.py write.py bash.py web_fetch.py
    output.py          # truncate + spill-to-file + continuation hints  (§6)
  context/             # L5
    estimate.py cap.py compact.py          # pair-safe cut points
  session/             # L6
    entries.py         # pydantic entry models, id/parentId tree
    store.py           # JSONL append + fsync + writer fencing
    query.py           # build_context_entries / build_messages
    fork.py resume.py
  delegation/          # L7
    spawn.py registry.py mailbox.py
  tui/                 # L8   own ~1.5k-line core — see design/aiden-ui-spec.md
    driver.py          # raw mode, alt screen, CSI ? 2026 synchronized output, mouse, kitty keys
    writer.py          # line-diff live-region writer (the interesting part)
    model.py           # Cell types: the transcript projection (§7)
    render.py          # rich segments → lines
    keys.py theme.py   # actions→chords (namespaced), 53 semantic colour tokens
    modes/             # inline (default) · review · transcript · sessions · plain · serve
  learning/            # THE REASON
    state.py           # HarnessEntry, RefinementEvent, ledger store
    refine.py          # gate model → writer model → apply
    triggers.py        # cadence F, warm-up W, explicit /refine
    probes.py          # usage counters: invoked/productive  (§4.4)
    consolidate.py     # idle-time merge/prune/decay → DREAMS.md
    proposals.py       # self-update: patch + rationale + eval report
  eval/                # cost axis + gates
    taskset.py runner.py oracle.py report.py
  observability/       # L9
    usage.py events.py otel.py
```

---

## 2. The loop (L3)

We adopt **Pi's shape** (smallest correct thing), with Claude Code's lesson that *the loop's real
work is recovery* ([research/01](../research/01-harness-anatomy.md) §loop).

```python
async def run_loop(prompts, ctx, cfg, emit, signal):
    pending = list(prompts) + await cfg.get_steering()
    while True:  # outer: follow-up resumption
        has_more_tools = True
        while has_more_tools or pending:
            if last_turn:  # ← compaction / model swap live here
                snap = await cfg.prepare_next_turn(last_turn)
                ctx, model_cfg = snap.ctx_or(ctx), snap.model_or(model_cfg)
                if not pending:  # compaction was slow; re-poll or drop input
                    pending = list(await cfg.get_steering())
            for m in pending:
                await inject(m)
                pending = []
            msg = await stream_assistant(ctx, cfg, signal, emit)
            if msg.stop_reason in ("error", "aborted"):
                return finish(msg)
            calls = [c for c in msg.content if c.type == "tool_call"]
            if calls:
                batch = (
                    fail_all(calls, "truncated arguments")  # stop_reason == "length"
                    if msg.stop_reason == "length"
                    else await execute(calls, ctx, cfg, signal, emit)
                )
                has_more_tools = not batch.terminate
            last_turn = TurnState(msg, batch, ctx)  # ONE frozen replace
            if await cfg.should_stop_after_turn(last_turn):
                return finish()
            pending = list(await cfg.get_steering())
        follow = list(await cfg.get_follow_up())
        if not follow:
            break
        pending = follow
```

Non-negotiables, each traceable to a measured failure:

| Rule | Why |
|---|---|
| `TurnState` is **one frozen dataclass**, replaced atomically | with ≥5 recovery `continue` paths, scattered field assignment is how state gets lost [10] |
| `stop_reason == "length"` → **fail every tool call** in the message | streamed args may be silently truncated; executing them is worse than an error [1] |
| `steering` and `follow_up` are **separate queues** | "interrupt this run" ≠ "extend a run that was about to end" [1] |
| re-poll steering after `prepare_next_turn` | compaction is slow; without this, typed input is dropped [1] |
| recovery paths are **named**, one module each | Goose keeps one file per continue path; discoverability is the point [10] |
| explicit `max_turns` + `cost_limit` with auto-report on trip | mini-SWE-agent ships `step_limit: 250`, `cost_limit: 3.`; unresolved runs are exactly the long ones [8][13] |

Aiden's first recovery paths: `retry_transport`, `compact_then_retry` (provider overflow),
`length_truncated`, `stuck_detected` (N identical failing edits on one file → inject a
strategy-change nudge rather than retrying harder; the measured cliff is 90.5% → 57.2% eventual
success after one failed edit [13]).

---

## 3. State hierarchy — the abstraction Aiden inherits

From Prime Agent's tech report [9], restated as our access rules:

| Level | What it is | Mutated by | Aiden owner |
|---|---|---|---|
| **L0 weights** | model | fine-tuning | nobody (out of scope) |
| **L1 active context** | the request we're about to send | compaction | `context/compact.py` |
| **L2 working state** | retained values, child sessions | *agentic GC* (model decides) | `delegation/`, `session/` |
| **L3 durable state** | transcripts, harness ledger, skills | **refinement (versioned)** | `learning/` |

- L1→L3 must be **lossy for the model, lossless for the system**: compaction replaces a prefix
  with a summary but the original entries stay retrievable ([research/01](../research/01-harness-anatomy.md),
  Prime §2.2 [9]).
- **The L1|L2 boundary is the interesting one.** Aiden's job at L2 is to make retained state
  *addressable and inspectable* (names, ids, sizes) without necessarily giving the model a REPL
  (ADR-07).
- Each L3 entry records which turn's evidence produced it, so any behaviour can be traced to a
  learning event — the requirement Prime's own Factorio incident made concrete [9][11].

---

## 4. Learning subsystem (the differentiator)

### 4.1 Harness state ledger

Schema is Prime Agent's, extended with usage probes and an eval link
([research/00](../research/00-reference-source-study.md) §6, [research/03](../research/03-continual-learning.md)):

```python
Kind = Literal["prompt", "memory", "skill", "subagent"]  # rules · facts · programs · coordination
Scope = Literal["local", "global"]  # session vs cross-session


@dataclass
class HarnessEntry:
    id: str
    kind: Kind
    title: str
    content: str
    path: str = "general"  # grouping
    scope: Scope = "local"
    version: int = 1
    source: str = "agent"  # agent | refine | human | import
    reference: dict = {}  # skill: {type: "python", import, callable|call_pattern}
    arguments: dict = {}  # skill: declared inputs
    metadata: dict = {}  # {"scope": "..."} blast radius; tags
    evidence: str = ""  # REQUIRED for create/update: what in the trajectory justifies this
    expected_outcome: str = ""  # what should improve + how to validate
    created_at: str
    updated_at: str


@dataclass
class RefinementEvent:  # append-only, one per review pass
    id: str  # refine_0001
    trigger: str
    changes: list[str]
    evidence: str
    outcome: str
    snapshot_before: str
    snapshot_after: str  # content hashes → rollback target
    eval_run: str | None  # ← Aiden addition: link to the gate result
    model: str
    harness_version: str
    state_hash: str
```

Storage: one JSON file for local (`sessions/<id>/harness/state.json`), one for global
(`~/.aiden/harness/state.json`). **Reload-on-mtime before every mutation** — Prime does this so
two writers (kernel + host) can't clobber each other; we get the same protection from the TUI and
the CLI both writing [11].

### 4.2 The refinement loop

Two model calls, not one ([research/03](../research/03-continual-learning.md), [11]):

```
        every F turns after warm-up W · on user correction · on repeated failure
        · on /refine · at session end
                                     │
                     ┌───────────────▼────────────────┐
                     │ GATE  (cheap model, ≤4k out)   │  should_refine? rationale? instructions?
                     │ rejects one-off noise, transient│
                     │ tool output, unsupported guesses│
                     └───────────────┬────────────────┘
                        no │                │ yes
                     discard         ┌───────▼──────────────────────────┐
                                     │ WRITER (≤32k out, JSON only)     │
                                     │ {summary, rationale,             │
                                     │  expectedOutcome, edits[]}       │
                                     └───────┬──────────────────────────┘
                                             ▼
                            APPLY at TURN BOUNDARY · validate each edit
                            at apply time (proposal is untrusted input)
                                             ▼
                 record RefinementEvent(+snapshots) → probe → EVAL GATE → keep | rollback
```

Hard rules, with provenance:

1. **`prompt` entries are supplemental. The base system prompt is immutable.** [11][9]
2. **CRUD only — never a whole-body rewrite.** ACE's *context collapse* is a measured failure of
   rewriting long self-referential contexts [3]. This is why `edits[]` are per-entry operations.
3. **Local by default.** Global writes require an explicit request; during a local refinement,
   global entries are read-only context — create a local override instead [11].
4. **Apply at a turn boundary**, never mid-turn [9].
5. **Deletion and demotion are required.** Prime/Continual-Harness delete components "not invoked
   productively" and demote stale memory importance [12][11] → needs §4.4.
6. **Every entry carries `evidence` + `expected_outcome`.** Unscored accumulation is the failure
   mode Prime's Factorio trace demonstrated: an *exploit was persisted as a reusable skill* [9].
7. **Rollback is a lookup**, from `snapshot_before` — never a rescue operation.

### 4.3 Routing heuristic (what kind does a lesson become)

| Evidence in the trajectory | Becomes |
|---|---|
| "I keep doing this multi-step procedure" | `skill` |
| "I keep spawning a child with the same role" | `subagent` |
| a durable fact / user preference / correction | `memory` |
| a narrow behavioural policy ("always run tests before claiming done") | `prompt` note |
| anything about **Aiden's own code/tools** | a **self-update proposal** (§4.5), not L3 state |

### 4.4 Usage probes — the part nobody ships

To honour rules 5–6 we must know whether a learned artifact helps. So every entry gets counters,
updated by the harness (not the model):

```
per entry id:  loads · executed · succeeded · user_overwritten_later · last_used
              · appears_in_low_score_run   (from eval/trajectory scoring)
```

`consolidate.py` uses them: `loads == 0 for K sessions` → candidate for delete; skill executed but
its runs score worse → quarantine + rollback. This is Aiden's cheap stand-in for the paper's
process-reward model [12] — we can't train a PRM, but we *can* correlate entries with outcomes
across runs of our private eval set.

### 4.5 Self-update pipeline (Aiden editing Aiden)

Two tiers, deliberately separated ([research/03](../research/03-continual-learning.md) §self-update):

```
TIER 1 — harness state (L3). agent-initiated, reversible, no code change.
  evidence → refine (gate+writer) → apply at turn boundary → probe → auto-rollback on regression

TIER 2 — Aiden's own source (tools, loop, prompt sections). ALWAYS human-gated.
  1. proposal.md      why + diff plan + which eval tasks it claims to improve
  2. branch           git worktree .aiden/worktrees/proposal-<sha>
  3. static gate      ruff · mypy · pytest                      (~10 s)
  4. smoke eval       15 tasks × 1 seed                          (~$1)
  5. full eval        N tasks × 3 seeds vs pinned baseline        (~$10)
  6. PROMOTE IFF      resolved_delta ≥ 0 AND cost_delta ≤ +15%
                      AND no new infra failures AND delta > 3 pp OR n ≥ 3 seeds paired
  7. human approve    diff + eval report rendered in the TUI
  8. tag → canary (own daily use) → auto-revert on regression
```

Boundaries (learned from the exploit-persistence incident [9]):

- **The Refiner cannot write code.** "Never edit source files directly" is a literal rule in
  Prime's refine prompt [11]; Tier 2 needs an approval event the tool layer enforces, not the
  prompt asks for ([research/01](../research/01-harness-anatomy.md) §permissions).
- **Least privilege per surface**: writes to `.aiden/harness/**` are allowed; to
  `aiden/learning/proposals/**` allowed-with-log; to `aiden/**` (own source) denied except inside a
  proposal worktree; to `.aiden/policy/**` and `config.py` **never**.
- **Evals are blind to the improver** — the writer model must not see the task oracles, and models
  change behaviour when they infer they're being evaluated [13].
- Every eval run records `state_hash` + `harness_version`, else a learning-driven change is not
  reproducible.

### 4.6 Consolidation (idle-time / "sleep-time compute")

Runs off the critical path ([4], [research/03](../research/03-continual-learning.md)): merge
duplicates, supersede stale facts, decay importance, promote/demote candidates→skills, prune
non-invoked subagent specs, and write a human-reviewable diary to `.aiden/dreams/DREAMS.md`.
Never promote untrusted provenance (a transcript line that is *itself* model-authored can't
justify itself). Sleep-time compute's measured shape — ~5× less test-time compute at equal
accuracy — is why this is the highest-value offline job [4].

---

## 5. Tool plane (L4)

Seven core tools, deferred plugins, output budgets tighter than every reference harness — see
[research/02](../research/02-tooling-efficiency.md) §2 for the full table and justification:
`read`, `grep`, `glob`, `edit`, `write`, `bash`, `web_fetch` (≈1.5–2.5k tokens of schema).
Deferred until discovered: `symbols`/`rename` (tree-sitter/LSP), `task` (delegation), `note`,
`skill`, `mcpScript`.

Execution contract, per tool:

```python
@dataclass(frozen=True)
class Tool:
    name: str; description: str; args: type[BaseModel]
    read_only: bool; concurrency_safe: bool
    max_output_chars: int; spill: bool          # >budget → file + pointer
    async def run(self, args, ctx, signal, emit_update) -> ToolResult
```

Guards in this order — each class of failure returns an **error tool result**, never raises, so the
model can see and correct it ([research/01](../research/01-harness-anatomy.md) §validation):

```
1 schema      pydantic TypeAdapter           → echo the offending field + a corrected example
2 semantics   path escapes cwd? binary?      → explicit refusal string
3 policy      before_tool_call → Allow|Block(reason)|Mutate   (deny beats allow; hooks can't
              upgrade permissions)
4 exec        with timeout + cancel
5 shaping     truncate/tail + CONTINUATION HINT + timing + counts
6 validate-out post-edit lint/parse gate     (SWE-agent: +3.0 pp from lint-on-edit alone [13])
```

- Parallelise only `read_only and concurrency_safe`; **emit results in call order** even when they
  complete out of order, to keep the transcript deterministic and the prompt cache warm [1].
- Empty output must be an explicit message ("ran successfully, no output") — a bare empty string
  reads as failure and derails agents [1].
- Patch format: **exact-string replace with unique-match requirement** as primary, whole-file
  `write` as fallback; unified diffs failed to apply on 51% of attempts even after repair [13]
  ([research/02](../research/02-tooling-efficiency.md) §4).
- Read-before-edit with **content-hash** staleness rejection (hash at 2.2 GB/s is cheap; mtime lies)
  ([research/02](../research/02-tooling-efficiency.md) §6).

---

## 6. Context lifecycle (L5) and output shaping

Two mechanisms only, in this order ([research/01](../research/01-harness-anatomy.md) §context):

```
   ingest-time            request-time
   caps + spill-to-file   estimate → compact? → send
   ┌──────────────────┐   ┌──────────────────────────────────────┐
r │ 16 KB / 400 lines  │   │ ctx_tokens > window − reserve(16384)?│
o │ per call; big bash │   └───────┬──────────────────────────────┘
u │ output → file+path │           │ yes
t │ grep: files-first  │   ┌───────▼─────────────────────────────┐
e │ 3 modes            │   │ cut point = walk back to             │
   └──────────────────┘   │   keep_recent(20k), NEVER between a   │
                          │   tool_call and its result            │
                          └───────┬───────────────────────────────┘
                                  ▼
                     structured summary (Goal · Constraints · Progress
                     done/active/blocked · Key decisions · Next steps ·
                     Files touched) appended as CompactionEntry,
                     previous summary passed as iterative context
```

Numbers adopted (with source per constant in `config.py`): `reserve_tokens=16_384`,
`keep_recent_tokens=20_000`, estimator `4 chars/token` (Pi) — all justified and cross-checked in
[research/09](../research/09-measured-evidence.md) §2. Provider-side `clear_tool_uses`-style
context editing and *prune-only* strategies are later options.

**Aiden-specific prototype:** a **per-turn aggregate output budget**. Every surveyed harness caps
per-call; none caps a 5-tool batch. That's a cheap experiment with a measurable payoff
([research/09](../research/09-measured-evidence.md) §2 note).

Search strategy: **ripgrep-first, no embeddings in core** — a context-free `rg` on a 104 MB corpus
is ≈90k tokens vs ≈650 for `-l`; iterative/vim-style search *scored below having no search tool at
all* [13] ([research/02](../research/02-tooling-efficiency.md) §3, §5). The Aider-style tree-sitter
+ graph-ranking repo map (~1k token budget) is the one retrieval idea to rebuild.

---

## 7. Sessions (L6): the entry tree is the product

Append-only JSONL, `id`/`parentId`, one leaf = current position
([research/01](../research/01-harness-anatomy.md) §persistence,
[research/00](../research/00-reference-source-study.md) §3):

```
SessionHeader · MessageEntry · ToolCallEntry/ToolResultEntry
ModelChangeEntry · ThinkingLevelChangeEntry
CompactionEntry{summary, first_kept_entry_id, retained_tail?, usage, tokens_before}
BranchSummaryEntry{…}                       ← what you ABANDONED when you forked
CustomEntry · CustomMessageEntry · LabelEntry
HarnessEditEntry{refinement_id, kind, op, entry_id, snapshot_before/after}   ← Aiden addition
ChildUsageAttributedEntry{parent_message_id, child_usage, aggregate}         ← from Prime
EvalLinkEntry{run_id, state_hash}                                            ← Aiden addition
```

`build_context_entries()` walks leaf→root honouring the latest compaction;
`build_session_context()` projects entries → provider messages. The TUI, the LLM request, resume,
fork and *the refiner's view of "recent trajectory"* are all projections of the same tree. Because
harness edits and child usage are entries, **a learning event is part of the transcript**, so
"why did Aiden do that" always has an answer. Git is the second store: every accepted edit is a
commit tagged with its prompt, so `/undo` is real ([research/01](../research/01-harness-anatomy.md)).

---

## 8. Delegation (L7) — designed now, mostly deferred

When subagents land they must be born with the right API, because the two rules that are hard to
retrofit are cheap to start with ([research/00](../research/00-reference-source-study.md) §5,
[11][9]):

1. **`spawn()` returns a handle at admission, never the answer.** Results arrive as messages or
   files. Blocking-join is the failure mode that inflates the parent's context.
2. **Child usage attributes to the parent *turn*, and tree reporting subtracts it**, so "own
   context" and "billable total" both reconcile.

Plus: `context ∈ {isolated, fork}` where `fork` requires a **byte-identical prefix** to share the
parent's prompt cache (else the copy is strictly worse than isolated)
([research/01](../research/01-harness-anatomy.md) §delegation); a depth counter with a loud failure
at the limit; and unknown options rejected rather than ignored.

**Expect width, not depth.** Prime's 7-day Factorio run produced **633 depth-1 subagents across
149 dispatch waves, ≤7 concurrent** — shallow and repeatedly widening [9]. Aiden's UI (the Agents
View analogue) and queue design should optimise for that shape.

---

## 9. RLM layer (ADR-07) — deferred, but its discipline is adopted now

Full "one persistent Python kernel as the only tool" is a **v2 experiment**, because the same
sources that praise RLM also document it *losing* to a plain Python tool on `math-python`, losing
without explicit strategy tips on DeepDive, and being **slower in every environment**
([research/04](../research/04-prime-intellect-rlm.md), [15]). We adopt the four transferable
invariants immediately:

| RLM invariant | Aiden v1 form |
|---|---|
| Context is a **variable**, not a scrollback | retained values are named, addressable, size-reported (L2 in §3) |
| Verbose work belongs to a **child** | `web_fetch` gets a second small model to compress; children never inherit the parent's scrollback [15] |
| Answers are **edited artifacts** | `answer`-style draft region in session state, not one-shot streaming |
| Hard **output caps force** programmatic processing | 16 KB/call, plus the per-turn aggregate budget in §6 |

The measurable prize stays attractive: +26% median over compaction, +130% over CodeAct-with-subcalls,
+13% over Claude Code [15] — on tasks where the input *doesn't fit*. Aiden's v2 gate is therefore
"long-context tasks only", with our own eval set deciding (see [research/05](../research/05-benchmarks-evals.md)).

---

## 10. Presentation (L8)

Owned by [design/aiden-ui-spec.md](../design/aiden-ui-spec.md). **ADR-09: we do not build on Textual.**
[research/06](../research/06-ui-ux.md)'s decision matrix rejects it for this shape of app: Textual owns
the alt buffer by default, which breaks native selection, tmux copy and `Cmd-F` on exactly the
surface an agent loop needs most, has no image support, and churns breaking changes every major.
Instead: **our own thin, inline-first core** using `rich` as the segment producer and
`prompt_toolkit` as the input/driver layer — the same pair Aider ships, and the same shape pi-tui
and Codex independently landed on. The `Cell → Lines` boundary keeps a Textual shell as an escape
hatch for a later three-pane "workspace" mode.

Architecture contract only:

- The UI consumes **events, never state**; it may not mutate anything except by enqueuing a command.
- Frame budget from Pi's renderer: `MIN_RENDER_INTERVAL_MS = 16` (~60 fps), coalesce
  `requestRender` via `nextTick`, and let **user input pre-empt** a throttled frame
  (`requestImmediateRender` cancels the pending timer); line-diff against `previousLines` inside
  **synchronized output** (`CSI ? 2026 h … l`) so no partial frame is ever visible. Width changes
  force a full redraw because wrapping changed
  ([research/00](../research/00-reference-source-study.md), [research/06](../research/06-ui-ux.md)).
- **Inline-first**: finalized cells are *inserted into the real scrollback* rather than re-rendered
  in an owned viewport; the live region holds only the mutable tail. Alt-screen modes
  (`review`, `transcript`, `sessions`) are transient and print their final document back on exit.
- Everything in §7 makes the UI informative for free: the transcript tree *is* the timeline view,
  `HarnessEditEntry` *is* the learning feed, `ChildUsageAttributedEntry` *is* the cost tree.

---

## 11. Accounting & observability (L9)

Minimum recorded set, justified per-source in
[research/09](../research/09-measured-evidence.md) §7 and
[research/02](../research/02-tooling-efficiency.md) §instrumentation:
per-message `usage{input, output, cache_read, cache_write}`, per-entry cost incl. **summarization**
and child usage, per-tool duration/exit/output-bytes/**bytes-truncated/re-paged**, failed-edit
chains per file, turn index of first edit↔run cycle and of submit, tool-invocation histogram,
`harness_version` + `state_hash` on every run, LLM-judged failure labels.

Export via **OpenTelemetry GenAI semantic conventions** so Langfuse/Braintrust/W&B stay swappable
([research/05](../research/05-benchmarks-evals.md) §tooling). Instrumentation lands *with* the loop,
not after it — it's the only way the self-update gate can ever fire.

---

## 12. Evaluation (the cost axis)

Design in [research/05](../research/05-benchmarks-evals.md); the three things that shape
architecture:

- **Inference and grading are separate halves.** Aiden writes `preds.jsonl`
  (`instance_id`, `model_name_or_path`, `model_patch`); the benchmark's oracle decides. That seam is
  what lets us swap harnesses without touching the answer key.
- **Private, repo-derived tasks are the real eval.** FeatureBench's 74.4% → 11.0% collapse on
  feature-level tasks is the argument [13]; our miner derives F2P/P2P from merged PRs, and only
  tasks newer than the model's cutoff are trusted.
- **`< 3 pp` is noise.** Infrastructure config alone moves Terminal-Bench 2.0 by 6 pp [14], and
  single-seed binomial CIs are already 1–2 pp. So: paired runs, ≥3 seeds, report `resolved @ cost`
  curves, and prefer behavioural metrics when the task set is small — because nanoGPT showed a
  harness effect that pass/fail *missed entirely* and behaviour metrics found [9].

---

## 13. Build order

| Milestone | Ships | Doc |
|---|---|---|
| M0 | transcript tree + JSONL store + replay-to-render | this §7 |
| M1 | transport + streaming + retry + usage | [research/01](../research/01-harness-anatomy.md) §transport |
| M2 | 7 tools with budgets, validation ladder, spill | [research/02](../research/02-tooling-efficiency.md) |
| M3 | **TUI** — the first goal: streaming turns, tool trace, diff review, cost HUD | [design/aiden-ui-spec.md](../design/aiden-ui-spec.md) |
| M4 | loop + compaction + session tree + steering | §2, §6 |
| M5 | harness ledger + `/refine` (gate+writer) + probes + `DREAMS.md` | §4 |
| M6 | eval runner + private task set + paired-report; **then** Tier-2 self-update | §4.5, §12 |
| M7 | subagents (handle-first API) → optional RLM kernel experiment | §8, §9 |

Rationale for M3-before-M4: the user-visible goal is the UI, and a UI built against a fake event
stream is cheap to retrofit; but M0 *must* precede everything because every other subsystem is a
projection of the transcript.

---

## Sources

Numbers are consolidated in [research/09-measured-evidence.md](../research/09-measured-evidence.md)
(its source list is authoritative; duplicated here for standalone reading).

- [1] Pi `@earendil-works/pi-coding-agent@0.85.1` — `pi-agent-core/dist/agent-loop.js`,
  `dist/core/tools/*`, docs `compaction.md`, `session-format.md`, `extensions.md`, `skills.md`, `tui.md`
- [2] SWE-agent: Agent-Computer Interfaces, arXiv:2405.15793 (ACI ablations)
- [3] *Agentic Context Engineering (ACE)*, arXiv:2510.04618 — grow-with-refine, context collapse
- [4] *Sleep-time Compute*, arXiv:2504.13171
- [5] Anthropic Engineering, *Writing effective tools for agents — with agents*
- [6] OpenClaw docs/source; Aider docs (repo map, edit formats); opencode; Goose; Codex CLI docs —
  as cited per-section in [research/01](../research/01-harness-anatomy.md) and
  [research/02](../research/02-tooling-efficiency.md)
- [7] OpenHands SDK docs (events, condensers, `FileEventStore`)
- [8] SWE-agent / mini-SWE-agent, https://github.com/SWE-agent/mini-swe-agent (budgets, SWE-bench recipe)
- [9] Karten et al., *Prime Agent: A Self-Improving RLM Harness*, arXiv:2608.23552
- [10] Claude Code internals/`query.ts` continue-paths and Goose `state_machine/` as compared in
  [research/01](../research/01-harness-anatomy.md)
- [11] Prime Agent source: `packages/coding-agent/src/core/refinement/refinement.ts`,
  `packages/coding-agent/docs/{rlm,rlm-runtime,architecture}.md`,
  `prime-agent-runtime/src/rlm/harness.py`
- [12] Karten et al., *Continual Harness: Online Adaptation for Self-Improving Foundation Agents*,
  arXiv:2605.09998
- [13] SWE-agent arXiv:2405.15793 (ablations, failure taxonomy, pass@k); FeatureBench; Aider
  edit-format completion rates — tabulated in
  [research/09](../research/09-measured-evidence.md)
- [14] Anthropic Engineering, *Quantifying infrastructure noise in agentic coding evals*
- [15] A. Zhang et al., *Recursive Language Models*, arXiv:2512.24601; Prime Intellect RLM blog
- [16] Agent Skills specification, https://agentskills.io ; SkillsBench (2026) as cited in
  [research/03](../research/03-continual-learning.md)
