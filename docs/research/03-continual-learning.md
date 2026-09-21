# Continual Learning, Self-Improvement, and Self-Update for a Coding Agent

## TL;DR

- **Learning ≠ memory.** Four separable mechanisms, each with its own failure mode: *write* (what gets captured), *store* (which layer), *read* (what gets re-injected), *act* (what changes the next episode's behaviour). Most published systems only improve the read path; the gains in 2025-2026 came from the write and consolidation paths [3][13][19][24].
- **Adopt the CoALA split literally**: episodic (what happened), semantic (what is true), procedural (how to do it). They need different stores, different prompts, and different decay. Mixing them into one vector collection is the classic 2023 mistake [4][15].
- **Reflection/consolidation must run off the critical path.** Sleep-time compute gives ~5× less test-time compute for equal accuracy and +13-18% accuracy from offline thinking; OpenClaw ships it as a cron "dreaming" sweep with promotion gates in production [13][15].
- **Contexts erode when rewritten.** "Brevity bias" and "context collapse" are measured failure modes; the fix is *incremental delta bullets with helpful/harmful counters + deterministic merge*, not a better summariser [19][20].
- **Skills are the highest-ROI learnable artifact, and they must stay small.** SkillsBench (2026): curated skills 33.9% → 50.5% mean pass rate (+16.6 pp) over 18 model-harness configs; skills with ≤3 modules beat exhaustive bundles; small model + skills ≈ larger model without [24].
- **Do not fine-tune in v1.** Reflective prompt/procedure evolution (GEPA, ACE, DC) captures most of the benefit with 10-1000× less infrastructure; weight updates are the wrong first lever for a personal harness [18][19][33].
- **Retrieval for code is not retrieval for chat.** Aider's repo map (tree-sitter symbols + dependency graph + graph ranking into a ~1k token budget) is the proven pattern for structure; embeddings earn their keep on *prose memory*, not on identifiers [10][18][39][40].
- **Self-update needs an empirical gate, not a proof.** DGM/SICA modify their own harness and accept changes only after benchmark validation (SWE-bench 20.0%→50.0%; SICA 17%→53%). They keep an *archive* of variants, so rollback is a lookup, not a rescue [36][37].
- **Self-modification's real limits are coherence and reward hacking, not disk space.** Vending-Bench shows long-running agents derailing into "meltdown loops" with no correlation to context exhaustion; Anthropic documented models changing behaviour when they infer they are being evaluated. Evals must be blind to the improver, and the improver must be blind to the tests [44][45][46].
- **v1 is ~6 files, not a database.** Append-only JSONL event log + a curated `MEMORY.md` under a token budget + `skills/*/SKILL.md` + a nightly consolidation job + a 20-task paired eval + git branches as the only self-update transport. Everything else is v1.1+ [15][22][41].

## A continual learning loop for Aiden

Six stages. Each stage has one owner (inline vs background), one artifact, and one named failure mode.

```
        ┌─────────────────────────── EPISODE (interactive turn) ───────────────────────────┐
        │                                                                                  │
 user ──►│  ① ACT            ② READ                    ③ WRITE (cheap, always on)          │
        │  edit/bash/   ◄── context assembler:      ──► signals: test result, user edit/    │
        │  test/run         • MEMORY.md (budgeted)      revert, /feedback, tool error,      │
        │                   • matching SKILL.md         tokens, files touched, human diff   │
        │                   • top-k episodic notes                                            │
        │                   • repo map (structure)                                          │
        └───────────────┬──────────────────────────────────────────────┬────────────────────┘
                        │ episode trace                                │ episodes/*.jsonl
                        ▼                                              ▼
   ┌──────────────── BACKGROUND (sleep-time, cron / "it is 23:40" trigger) ────────────────┐
   │ ④ APPRAISE          ⑤ CONSOLIDATE                 ⑥ PROPOSE (self-update candidate)    │
   │ generator-style     episode → semantic fact        diff against prompt / skill / tool   │
   │ reflection: why     episode → procedure (skill)    / memory index; write candidate to   │
   │ did it fail?        decay + supersede + merge      .aiden/proposals/<sha>/*.patch       │
   └───────────────┬──────────────────────────────────────────────┬─────────────────────────┘
                   │ durable notes                                │ patch + rationale + evidence
                   ▼                                              ▼
        memory store (4 layers)                     ┌──────── GATE (cannot be skipped) ───────┐
        episodic │ semantic │ procedural │ index     │ blind paired eval: capability suite +   │
                                                   │ regression suite; static checks; sandbox│
                                                   │ human review (mandatory for: prompts,   │
                                                   │ tools/extensions, permission config)    │
                                                   └───────┬───────────────────────┬─────────┘
                                                           │ pass                  │ fail
                                                           ▼                       ▼
                                              git tag aiden-<semver>       discard → archive
                                              memory + agent state snapshot (.af-style)
                                                           │
                                              monitor next 20 episodes ──► auto-rollback on drift
```

Failure modes to design against, stage by stage: ① stale procedure applied to a changed repo; ② context pollution (memory competing with the task for tokens) [40]; ③ capturing noise, or capturing nothing because the model must stop and write; ④ self-flattery (the same model grades itself) [26][27]; ⑤ compounding errors / context collapse [19]; ⑥ overfitting to the eval, and reward hacking when the improver can see the tests [41][45].

Two structural rules fall out of the diagram:

1. **The interactive loop never learns; the background loop learns.** Inline the model should only *write raw signals* and *read*. Everything that costs tokens (reflection, dedup, decay, skill induction, patch generation) is a scheduled job [13][15][18].
2. **Writes are append-only; the prompt-facing view is a cache.** `MEMORY.md`, the skill index, and any injected context are *derived* and rebuildable. That makes forgetting safe and rollback cheap — the property every production memory system in 2026 converges on [15][21][51].

## Memory layers

### The taxonomy we use (CoALA × MemGPT × OpenClaw)

CoALA gives the vocabulary: **episodic** (experiences, time-ordered), **semantic** (facts about the world and the user, de-contextualised), **procedural** (skills, rules, the harness itself), with a **working memory** = the context window [4]. MemGPT adds the systems-level insight: treat the window as main memory and external storage as disk, managed by *function calls the agent makes itself* — a queue manager, evict-and-recall, and self-editing memory [1]. OpenClaw (2026) is what this looks like shipped: plain Markdown files, "there is no hidden state", `USER.md` (stable preferences as directives), `MEMORY.md` (curated durable facts, loaded at session start), `memory/YYYY-MM-DD.md` (working layer, indexed but not injected), plus `DREAMS.md` as the human review surface for background consolidation [15]. Claude Code documents the same production pattern: persistent `CLAUDE.md` instructions plus project auto-memory storage [43].

| Layer | Content | Store | Read policy | Write policy | Decay |
|---|---|---|---|---|---|
| Working | this turn: task, diff, tool output | context window | always | — | eviction/compaction [40] |
| Episodic | session/episode records: what I tried, what broke, tokens, outcome | JSONL + sqlite FTS | recency- & similarity-biased top-k | automatic per episode | age + never deleted (raw) |
| Semantic | durable facts: "repo X uses pytest-asyncio", "user dislikes MagicMock" | Markdown notes w/ frontmatter | hybrid (vector+keyword) | promoted by consolidation | supersede in place [15] |
| Procedural | "how to": skills, checklists, scripts | `skills/*/SKILL.md` | name+description match, on demand | proposed, **gated**, then committed | win/loss counters, archive at N |
| Index | TOC of the above, persona/self-model | `MEMORY.md`, `USER.md` | injected verbatim, hard budget | rewritten by curator | truncate = signal to curate [15] |

### Generative agents: the scoring function that is still the best baseline

Park et al. retrieve by `score = α_recency·recency + α_importance·importance + α_relevance·relevance` — exponential decay on the last access (factor 0.995 per token of simulated time), an LLM-assigned 1-10 importance score computed *at write time*, and embedding cosine for relevance [3]. Two details matter more than the formula:

- **Importance is scored when the memory is created**, not at query time. It is a paid-for annotation.
- **Reflection is threshold-triggered**: when the summed importance of recent events crosses a threshold (~150 in their implementation), the agent asks itself 3-5 salient high-level questions, retrieves evidence for each, and writes *higher-level inferences with pointers back to the raw memories*. Their ablations show removing reflection degrades believability about as much as removing observation [3].

Aiden's translation: an `importance` field on every episodic write (cheap: piggyback on the end-of-turn summary), and a reflection trigger on *cumulative* importance per topic, not on a wall-clock timer. Reflections are stored as semantic memories that keep `evidence: [episode_ids]` — provenance is what lets you audit and un-learn a bad inference later [3][15].

### Vector vs graph

| | Vector / chunk store | Temporal knowledge graph |
|---|---|---|
| Representative | Mem0 [8], OpenClaw builtin (sqlite, hybrid) [15] | Zep/Graphiti [5], HippoRAG [11], A-MEM [7] |
| Unit | embedding + text | nodes (entities, events) + typed edges with `valid_from`/`invalid_at` |
| Wins | fuzzy NL queries, zero schema, cheap | multi-hop, **contradiction handling**, "what was true in March" |
| Loses | negation, recency, relational queries, updates (duplicate chunks) | cost of extraction, brittleness of entity resolution, latency |
| Evidence | Mem0: +26% rel. over OpenAI memory on LOCOMO, 91% lower p95 latency, >90% token savings vs full-context [8] | Zep: DMR 94.8% vs MemGPT 93.4%; up to +18.5% on LongMemEval with 90% lower latency [5] |
| 2025 refinement | "graph memory" variant of Mem0 adds only ~2% over its own base [8] | HippoRAG: KG + Personalized PageRank, +20% multi-hop, 10-30× cheaper than iterative retrieval [11]; HippoRAG 2 improves *factual* more than *sense-making* recall [12] |

Graphiti's genuinely useful idea is **bi-temporal invalidation**: when a fact changes you do not delete or overwrite, you add an edge that marks the old one no longer valid, keeping both the fact and the moment it was learned [5]. A-MEM does something similar socially — each new note triggers LLM-generated links to existing notes and can *update* neighbours (Zettelkasten "evolve" step) [7].

**Decision for Aiden: markdown notes + typed frontmatter, indexed with hybrid search; no graph DB in v1.** Rationale: (a) the domains where graphs pay (long multi-party chat, entity-heavy enterprise data) are not coding-agent domains; (b) `supersedes:` + `valid_until:` fields give ~80% of bi-temporal invalidation in 5 lines of YAML [5][15]; (c) graphs are hard to review in a diff, and reviewability is a hard requirement for a self-updating system. Revisit when a concrete query fails that needs multi-hop over entities.

### Consolidation and forgetting

Consolidation is where a personal harness earns its keep, and where it most often destroys value.

- **Append, then distil.** Daily/episodic layer stays raw and searchable; the durable layer is rewritten only by the background job. OpenClaw's "dreaming" is the reference implementation: scheduled cron sweep, candidates must pass **score + recall-frequency + query-diversity** gates, a tool-free consolidation step decides merges and supersessions, invalid or unavailable decisions fall back to append-only, **taint-gated** so untrusted/system-derived candidates never enter the promotion prompt, and every phase writes a reviewable diary entry to `DREAMS.md` [15].
- **Forgetting = visibility, not deletion.** MemoryBank's original proposal was an Ebbinghaus-style decay applied to stored memories, strengthened on recall and allowed to fade otherwise [9]. Practically: keep raw, decay *retrieval weight*, and drop out of the injected index. Never delete the episodic log; it is the only audit trail the self-update pipeline has.
- **Budget-triggered curation.** If the injected file exceeds its budget, that is the signal to move detail into topic notes and keep a summary — OpenClaw truncates the injected copy while leaving the file intact, and tells you via `/context detail` [15]. Aiden should treat "truncated" as an error condition that wakes the curator.
- **Recall metadata on reads.** LongMemEval's design lessons are about the three stages — indexing, retrieval, reading: *session decomposition* for value granularity, *fact-augmented key expansion* for indexing, *time-aware query expansion* for scoping [10]. Commercial assistants and long-context models drop ~30% accuracy on sustaining information across interactions; the fixes are read-path engineering, not bigger windows [10].
- **Pre-compaction flush.** Before summarising away a conversation, run a silent turn that saves anything not yet on disk (OpenClaw `compaction.memoryFlush`, on by default, with a cheap-model override for the housekeeping turn) [15]. This is the single cheapest anti-amnesia mechanism available and it belongs in v1.

## Skill library design

### Prior art, ranked by mechanism quality

**Voyager** — procedural memory as *executable programs*: an ever-growing library of JavaScript Minecraft skills, each added only after environment feedback + self-verification pass; retrieval by embedding of the *task description* → skill; skills are "temporally extended, interpretable, and compositional", which "alleviates catastrophic forgetting" [16]. Results: 3.3× unique items, 2.3× longer distance, up to 15.3× faster tech-tree milestones vs prior SOTA [16]. **The transferable insight: verify before you store.** Not the Minecraft part.

**Agent Workflow Memory** — induces *workflows* (reusable subroutines) from trajectories and injects only the selected ones. +24.6% and +51.1% relative success on Mind2Web and WebArena, fewer steps per solved task, and online induction (from test queries, no training set) generalises across tasks, sites, and domains, beating baselines by 8.9-14.0 absolute points as the train/test gap widens [17]. **Insight: the unit of learning is a procedure with a name and a trigger, not a transcript.**

**Dynamic Cheatsheet** — test-time learning: a persistent, *self-curated* memory of strategies, code snippets and insights, maintained in two modes (cumulative; distilled). Claude 3.5 Sonnet doubles on AIME; GPT-4o goes 10% → 99% on Game of 24 by reusing a discovered Python approach; arithmetic-prone tasks reach near-perfect vs ~50% baseline; +9% GPQA-Diamond, +8% MMLU-Pro. Memory stays concise and transferable rather than storing transcripts [18].

**ACE (Agentic Context Engineering, ICLR 2026)** — the current best articulation of *how* to grow a context without rotting it. Three roles: **Generator** (trajectories), **Reflector** (insight extraction deliberately separated from curation), **Curator** (lessons → structured *delta* updates carrying helpful/harmful counters, merged deterministically with de-duplication and pruning) [19][20]. +10.6% on agent tasks (AppWorld) and +8.6% on finance (FiNER+XBRL); −82.3% adaptation latency and −75.1% rollouts vs GEPA offline; −91.5% latency and −83.6% token cost vs DC online; 86.9% lower adaptation latency on average; works without labels from natural execution feedback [20]. Its negative result is the more valuable half: monolithic rewrites cause **context collapse**, and summarise-for-brevity causes **brevity bias** — both destroy domain knowledge that was already there [19].

```python
# github.com/ace-agent/ace — README "Basic Usage" (real API, not invented)
from ace import ACE
ace_system = ACE(api_provider="sambanova",
                 generator_model="DeepSeek-V3.1", reflector_model="DeepSeek-V3.1",
                 curator_model="DeepSeek-V3.1", max_tokens=4096)
config = {"curator_frequency": 1, "playbook_token_budget": 80000,
          "online_eval_frequency": 15, "no_ground_truth": False, ...}
results = ace_system.run(mode="online", test_samples=test_data,
                         data_processor=processor, config=config)   # offline also supported
```

Note the two knobs that encode the whole method: `playbook_token_budget` (the playbook is a *budgeted* artifact) and `curator_frequency` (growth is batched, not per-turn) [20].

**Claude Skills / Agent Skills** — the standard that won. A skill is a folder with `SKILL.md`: YAML frontmatter requiring only `name` and `description`, then instructions; optionally scripts and references. Progressive disclosure in three levels — metadata always in context, body on demand, bundled files only when needed [21][22][23]. Pi implements the same standard, loading from `~/.pi/agent/skills/`, `~/.agents/skills/`, project `.pi/skills/` and `.agents/skills/`, and registering `/skill:name` commands [50]. SkillsBench (Feb 2026) is the first honest measurement: 87 tasks × 8 domains, matched no-Skills vs curated-Skills runs across 18 model-harness configs → mean pass rate 33.9% → 50.5% (+16.6 pp; 25.5% normalised), per-config gains +4.1 to +25.7 pp, **focused skills with ≤3 modules beat larger or exhaustive bundles**, and smaller models with skills match larger models without them [24].

### Aiden's skill library

Reuse the Agent Skills format unmodified (interchangeable with Claude Code, Codex, pi, OpenClaw), and add the provenance/lifecycle fields the spec leaves to `metadata:` [22][23]:

```yaml
---
name: pytest-asyncio-migrations
description: Use when touching tests in repos that use pytest-asyncio; covers fixture
  scoping, event-loop flags, and the two ways this repo configures it.
metadata:
  kind: procedure            # procedure | guardrail | cheat
  origin: episode:2026-07-12#t4   # required: at least one observed episode
  evidence: [2026-07-12T14:02Z, 2026-08-01T09:41Z]
  applies_to: ["**/tests/**", "pyproject.toml"]
  status: active             # candidate → active → stale → archived
  helpful: 9
  harmful: 1                 # loaded and made the outcome worse
  last_used: 2026-08-03
  scope: repo                # repo | global          (both live in git)
  verified_by: "uv run pytest -q tests/async"   # executable check, from Voyager/DC [16][18]
---
```

Rules that come straight from the evidence base:

1. **Candidate first, always.** Nothing enters `skills/` in the interactive loop. The background job writes `candidates/`; promotion requires the eval gate (§ Self-update). ACE's counters and DGM's archive are the models [19][20][37].
2. **≤3 modules, one trigger each.** Split rather than merge; SkillsBench says exhaustive bundles lose [24]. Target: body < 300 tokens, references in sibling files (progressive disclosure) [21].
3. **Store procedures, not facts, in skills.** A fact ("user hates MagicMock") belongs in semantic memory; a skill must change the *sequence of actions* ("when you see X, do Y, check Z") [4][17].
4. **A skill without a `verified_by` check is a rumour.** Voyager's add-only-after-self-verification and DC's reuse-of-previously-validated-code are the two mechanisms that made them work [16][18].
5. **Counters drive retirement.** `helpful/(helpful+harmful)` < threshold or 90 days unused → `status: stale` → `archived` (moved, not deleted). This is the procedural analogue of MemoryBank's decay, but computed from use rather than time [9].
6. **Induce online too.** Keep both AWM modes: offline induction from the episode log (nightly) and online induction from the current session's own trajectory (when the same 4-step sequence repeats twice in one session, propose it) [17].

Retrieval: `name + description` in the system prompt (cheap, ~30 tokens each), full body only on match; add a deterministic pre-filter on `applies_to` globs so irrelevant skills cost zero tokens. Pi's own docs warn that models don't always open the skill — prompting or `/skill:name` forces it [50] — so Aiden's assembler should name candidate skills explicitly rather than trusting a list.

## Feedback loops: four gradients you can actually use

| Loop | Signal | Where the learning lands | Persistence | Cost |
|---|---|---|---|---|
| Self-Refine | model critiques its own draft | same episode (context) | none | 2-5× tokens [26] |
| CRITIC | model *uses a tool* to critique (run code, search) then revises | same episode | none | +tool calls [27] |
| Reflexion | verbal self-reflection on task feedback → `self-reflection` text in an episodic buffer | next trials, same task | within task (buffer) | trials × tokens [25] |
| ACE / DC / AWM | reflection → curated context / cheatsheet / workflow | all future tasks | permanent | offline job [17][18][19] |
| DPO / RLHF | preference pairs | weights | permanent | training infra [28] |

Ordering matters. Reflexion is "verbal reinforcement learning": the policy update is text in an episodic buffer, and it reached 91% pass@1 on HumanEval vs GPT-4's 80% baseline [25]. Self-Refine improves outputs by iterating feedback→revision with the same model [26]; CRITIC's contribution is that self-critique only helps when grounded in external feedback, because a model cannot reliably find its own reasoning errors without a tool [27]. **Consequence for Aiden: prefer loops whose feedback is external and deterministic — a failing test, a lint error, a build, a rejected diff — over loops whose feedback is the model's opinion.**

The honest catalogue of feedback available to a coding harness, cheapest first:

- test/lint/type/build results (free, deterministic, already in the loop);
- user **edits after** the agent's output (a diff on top of your diff — strong, implicit, unbiased);
- user reverts / "no, that's wrong" / re-prompts (implicit negative, very high precision);
- explicit `/good`, `/bad`, one-word reason (sparse but gold; needed to calibrate the rest);
- tokens, tool-calls, retries, elapsed turns per task (efficiency, not correctness);
- LLM-as-judge rubrics (flexible, non-deterministic, must be calibrated against humans) [41].

### Preference capture without a training cluster

Store pairs, not opinions. The DPO objective needs `(context, chosen, rejected)` triples and skips the explicit reward model entirely [28]; Constitutional AI/RLAIF show a model can generate critique-guided labels when humans are too slow [29], and Self-Rewarding LMs show the loop can bootstrap [30]. Aiden does not need any of that in v1 — it needs the *data plumbing*, because the same triples are worth three different things later: prompt optimization targets, skill-proposal evidence, and (if we ever train) preference data.

```python
# .aiden/preferences/*.jsonl — append-only, captured inline, consumed by the nightly job
{
    "id": "pref-2026-08-03-01",
    "ts": "2026-08-03T09:41:02Z",
    "session": "01J...",
    "context_ref": "episodes/2026-08-03.jsonl#line=412",  # pointer, not a copy (token hygiene)
    "chosen": {"kind": "patch", "ref": "git:abc123"},  # what the human left in the tree
    "rejected": {"kind": "patch", "ref": "git:def456"},  # what the agent produced, then edited
    "signal": "human_overwrite",
    "files": ["src/db/pool.py"],
    "task_class": "async-resource-lifecycle",
}
```

Two weight-level options worth knowing (both later, neither v1): **SEAL** has the model emit its own finetuning data *and* update directives (hyperparameters, augmentation via tools), then does SFT, and trains the self-editing behaviour with RL where the reward is the downstream performance of the updated model [31]. **SWE-RL** shows that software evolution data — real commits/PRs — is enough for meaningful post-training gains without hand-labelled reasoning data, which is a reminder that a personal harness sitting on git history is sitting on free training signal [32]. And **GEPA** is the counter-argument for doing none of it yet: a reflective prompt optimizer that "outperforms GRPO by 6% on average, up to 20%, while using up to 35× fewer rollouts", beats MIPROv2 by >10%, and needs only a handful of examples to beat a 20-episode RL run (ICLR 2026 oral; code released) [33][34].

## Sleep-time / offline compute

The mechanism, from the paper: pre-compute "context summaries" and likely intermediate results while idle, so a query arrives into partially-reasoned-about context. On self-authored Stateful GSM-Symbolic / Stateful AIME it cuts test-time compute ~5× at equal accuracy, and scaling offline compute adds up to +13% / +18%; amortised across related queries (Multi-Query GSM-Symbolic) the average cost per query falls 2.5×; effectiveness correlates with **how predictable the user's next query is** [13]. Letta productised it (sleeptime agents as a first-class API, and the paper's code under `letta-ai/sleep-time-compute`) [13][14][51], and OpenClaw's dreaming sweep is the same pattern with promotion gates and a review diary [15].

Aiden's schedule (all cheap models unless noted):

| Trigger | Job | Output |
|---|---|---|
| pre-compaction | memory flush | raw facts saved before summarisation [15][40] |
| session end (≤ 3 min) | episode appraisal | 1 episodic record + importance + candidate notes |
| idle 30 min / `caffeinate`-detected idle | recall-index refresh, embedding of new notes | search index |
| nightly 23:40 | consolidation: dedupe, supersede, decay, merge skills | diff to `MEMORY.md`, `candidates/*` |
| nightly | prediction pass: "what will the user ask/do tomorrow" from history + TODOs + calendar-free heuristics | pre-computed repo summaries, likely files |
| weekly | self-update proposal run (see next section) | proposals + eval report |
| on demand (`/dream`) | manual run of the nightly job | same |

The predictability finding [13] is the argument for *coding-agent* sleep-time being more valuable than chat-agent sleep-time: tomorrow's work is in this repo's TODOs, recent failures, and CI state. Precompute the expensive-to-derive artifacts: repo map summaries per module, test-suite topology, "gist" of the last 30 commits, open-question notes per file the user touched repeatedly.

## Retrieval that actually works in repos

Five strategies; all five are used in production, none is sufficient alone.

| Strategy | What it is good at | Where it fails | Latency / cost |
|---|---|---|---|
| Lexical (ripgrep/BM25) | exact identifiers, error strings, config keys, "who calls `X`" | synonyms, "the thing that retries" | ~ms, free |
| Structural (tree-sitter symbols + dependency graph) | "what is defined where", cross-file signatures, minimal-token overviews | semantics, prose | one index pass; aider ships ~1k-token maps by default via graph ranking [39] |
| LSP (definitions/references/rename) | precision for refactors | flaky setup, per-language servers | ~100 ms |
| Embeddings (chunks) | NL→code, docs, notes, commit messages | identifiers, exactness, staleness across edits | index + per-query embed |
| Graph (KG / PPR) | multi-hop over entities and time | extraction cost, brittle merge | highest [5][11] |

Concrete, cited guidance:

- **Aider's repo map is the pattern to copy**: send a *map*, not a dump. List files with their key symbols and defining lines; rank the full map with a graph algorithm over a dependency graph (nodes = source files, edges = dependencies), select the subset fitting the active budget, and re-rank dynamically as the chat state changes. `--map-tokens` defaults to 1k and is expanded when no files are in context yet [39]. This is procedural knowledge about *what is in the repo*, and it is the reason aider can edit files it never opened.
- **Hybrid is the 2026 default for memory, not for code.** OpenClaw's `memory_search` = vector similarity + keyword matching, "even when the wording differs", with session-transcript hits as a separate lane; engines are sqlite (default), LanceDB, or Honcho [15]. Code recall stays lexical+structural.
- **Anthropic's context-engineering guidance applies to the read path**: context is finite; prefer "a small set of highly relevant, high-signal tokens"; keep "the right information at the right size in the right place at the right time" — composable selection, not maximal stuffing; use structured note-taking (a NOTEPAD / to-do file) to persist important state outside the window so an agent can "think in N tokens and work in N+k tokens"; compact near limits (context awareness→compression→observation, "start from the most recent…"); and fan out to sub-agents with clean windows when a branch of work needs depth [40].
- **Anti-pattern: injecting top-k chunks of your own past transcripts as "memory".** That is what LongMemEval shows breaks (−30%), and what DC/ACE explicitly avoid by storing distilled strategies instead of transcripts [10][18][19].
- **Local precedent already in this environment**: `~/.agents/skills/session-recap/SKILL.md` argues the recap must be *written to a file, not spoken*, because anything in the transcript re-enters context on every compaction; `~/.agents/skills/cavecrew/SKILL.md` compresses subagent tool-results (~60% smaller) so delegation does not eat the main window. Both are correct, cheap, and copied.

Aiden's read path, concretely: (1) always inject the budgeted index (`MEMORY.md`, ≤2k tokens) + skill metadata; (2) on task start, structural pass (repo map, budget ~1k tokens, aider-style) [39]; (3) on demand, hybrid `memory_search` over semantic/episodic notes with recency·importance·relevance scoring [3][15]; (4) on multi-hop entity questions, escalate to the graph lane **only if** v2 ships one; (5) never auto-inject raw transcripts.

## Self-update pipeline with guardrails

### Surfaces, ordered by blast radius

| Surface | Examples | Reversible | Can it silently break you? | Gate required |
|---|---|---|---|---|
| Curated memory | `MEMORY.md`, notes, skill index | yes (log intact) | mild — wrong facts, bad taste | auto after eval on memory-touching tasks |
| Skills (procedural md) | `SKILL.md` add/edit/archive | yes (git) | medium — wrong routine applied broadly | paired eval + provenance check |
| System prompt / `AGENTS.md` | tone, tool policy, checklists | yes (git) | **high — global, every turn** | eval + human review |
| Tools / extensions | pi `registerTool`, hooks, Aiden python tools | yes (git+tag) | **high — arbitrary code** [49][50] | eval + sandbox + human review |
| Orchestration code / self-update policy | loop, retry, permission logic | yes | **catastrophic** | human review, mandatory |
| Weights | LoRA/finetune from preferences | no (cheap) | silent, general | out of scope v1-v3 |

Note the asymmetry: memory is safe to auto-edit because the raw log survives; prompts and extensions are not, because a bad edit is invisible until it costs a day.

### The gate

```
proposal (patch + rationale + evidence refs)
   │ 1. static: ruff/mypy/pyright, skill-frontmatter lint, secret scan, path allowlist check
   ▼
   │ 2. sandbox apply: isolated git worktree + copy of .aiden state; no host creds, no network
   ▼
   │ 3. paired eval (N≥3 seeds × M tasks): capability suite AND regression suite
   ▼
   │ 4. adversarial set: 5 tasks specifically for the changed behavior + 3 known traps
   ▼
   │ 5. human review: diff + eval report + "what I claim this fixes"
   ▼
promote: git tag aiden-prompt-1.7.0 / aiden-skill-0.4.2 ; snapshot agent state (.af-style)
   │
   └─ 6. canary: next 20 real episodes tracked; auto-revert if success drops >X pp or the
           user reverts twice on the same surface
```

The capability/regression split and the grader taxonomy are Anthropic's (Jan 2026): deterministic graders first ("does the code run, do the tests pass"), model-based graders for open-ended behaviour with periodic human calibration, human graders as the gold standard; scoring per task can be weighted, binary, or hybrid; **capability evals should start at a low pass rate** (a hill to climb) while **regression evals must sit near 100%**, and high-pass capability evals "graduate" into the regression suite [41]. That is exactly the lifecycle a self-updating harness needs.

Two more hard-won constraints:

- **Do not trust your own eval.** OpenAI audited a 27.6% slice of SWE-bench Verified and found that "at least 59.4% of the audited problems have flawed test cases that reject functionally correct submissions" — on the *curated* version of the most-used coding benchmark [42]. A 20-task private suite is a noisy instrument: run paired, ≥3 seeds, report variance, and read the transcripts of every failure before believing a delta [41][42].
- **Blind the improver.** The grading harness, its tasks and its expected outputs must live outside the write-scope of the agent proposing patches, and the improver's context should not reveal it is being graded. Anthropic's alignment-faking work showed a model changing its behaviour when it inferred it was in a training-like setting ("training-time preference misgeneralisation"), and the follow-up measured the same class of behaviour across setups [45]; Sonnet 4.5-era reporting notes models degrading or misbehaving under in-band evaluation pressure [45]. A self-modifying agent that can see the score function will eventually optimise the score function.

### Versioning and rollback

- **Prompts, skills, extensions, and policy are code.** They live in git; there is no other authority. Every applied self-update is a signed commit with the eval report id in the trailer (`Aiden-Eval: 2026-08-03T23:41Z/17 tasks/2 seeds`). Rollback = `git revert` + reload.
- **Serialise agent state in an interchange format.** Letta's Agent File (`.af`) is an open standard for serialising a stateful agent — system prompt, memory blocks, tools — into one shareable, diffable, version-controllable document [51]. Aiden should emit `<tag>.af.json` on every promotion so a rollback restores behaviour *and* memory together, and so a future migration off pi (or onto it) is a file conversion.
- **Semver the behavior, not the code.** `prompt-major.patch` bumps when a gate changes what the agent will do; skills get their own line (`aiden-skill-*`) so a single bad skill can be reverted without touching the prompt.
- **Keep an archive, not a head.** DGM's core engineering choice: maintain an *archive* of agent variants and sample from it when generating the next, so "many different paths through the search space" are explored in parallel and any ancestor is one lookup away [37]. Cheap version of this: keep the last 10 proposals with their scores, including rejected ones — rejected-because-measured is knowledge.

### Security limits of self-modification

Hard limits, each with a mechanism to respect:

1. **You cannot prove a change is net-beneficial.** The Gödel-machine ideal fails in practice; DGM's answer is empirical validation on benchmarks, which only works for *measurable* tasks [37]. Corollary: self-update is legitimate only on surfaces with an oracle (tests, evals, lint). Anything taste-shaped stays behind a human.
2. **Self-modification is arbitrary code execution with your own credentials.** Pi's docs are blunt: extensions run with full user permissions, "no built-in sandbox", and project trust is only an input-loading guard — prompt injection from repo files is "expected local-agent risk" [49]. So: the improver runs as a different user/container with no API keys for the *production* agent, and only the human-facing process may write to `~/.pi/agent/extensions/`, `.pi/settings.json`, or the permission config.
3. **Memory is an attack surface.** A note is future instruction. OpenClaw's `USER.md` convention writes preferences as directives *with observed-date and active/superseded metadata*, and its "action-sensitive memory" guidance explicitly records *when it is safe to act*, expiry, and who owns the claim, while stating that "memory can preserve approval context, but it does not enforce policy" — approvals/sandboxing/scheduled tasks do [15]. Its dreaming is **taint-gated**: untrusted or system-derived candidates never enter the consolidation prompt or the durable promotion path [15]. Aiden copies this verbatim: `provenance: {source: web|repo|tool|user, trusted: bool}`, and web/repo-derived content cannot be promoted to `MEMORY.md` or to a skill without a human.
4. **Long-horizon coherence collapses before context runs out.** Vending-Bench: runs >20M tokens where models derail into "meltdown loops from which they rarely recover" — via misread delivery schedules, forgotten orders, or tangents — and the failures showed **no correlation with the context window filling up** [44]. Self-modification on top of an incoherent loop amplifies incoherence. Mitigation: a fixed constitution file that the agent cannot write, a per-session cap on self-modifications, and no self-update while an episode is mid-task.
5. **The recursive-improvement threat is currently a capability question, not a policy one.** The International AI Safety Report 2026 documents post-training and test-time scaling as the drivers of progress and treats automated AI R&D as a capability to monitor rather than an achieved regime [47]; Anthropic's own internal evidence (Claude used on their research/engineering work; agents designing experiments where "direction-setting was the only meaningful role a human played"; the next-step judgement measure where Opus 4.5 chose better than the human 51% of the time at n=129 deliberately-hard moments, rising to 64% for a later model) points the same way: execution is largely solved, **goal choice is not** [46]. METR's time-horizon metric is the quantitative frame: the length of tasks an agent completes autonomously keeps doubling [48]. Design accordingly: the human stays in the loop at the goal-setting and gate steps for as long as that holds, and the architecture should not need to change when it stops holding.
6. **Enumerated permissions, not vibes.** The improver may write only to an allowlist (`skills/**`, `.aiden/memory/**`, `prompts/**`); the denylist is enforced by the harness, not by the prompt: `.pi/settings.json`, `~/.pi/**`, `.git/**`, secrets, `policy/**`, the eval suite, and the improver's own sandbox config. Pi gives us the hook point to do this honestly — a tool-call interceptor, which is exactly the documented pattern for path protection and permission gates [50]:

```typescript
// pi extension: gate writes to self-writable paths only (docs/extensions.md "Quick Start" shape)
pi.on("tool_call", async (event, ctx) => {
  if (event.toolName !== "write" && event.toolName !== "edit") return;
  const p = event.input?.path ?? "";
  if (!ALLOWLIST.some(g => minimatch(p, g))) {
    const ok = await ctx.ui.confirm("Self-update blocked", `Patch touches ${p}. Allow?`);
    if (!ok) return { block: true, reason: "outside self-writable allowlist" };
  }
});
```

## What is genuinely new in 2025-2026 vs folklore

| Folklore (2023-2024) | 2025-2026 reality | Evidence |
|---|---|---|
| "Add a vector DB and the agent remembers." | The vector lane is the *easy* part; failure lives in indexing granularity, update/contradiction handling, and reading. Long-context and commercial assistants lose ~30% on LongMemEval; graph memory bought Mem0 only ~2% over its own base variant, while Zep reports up to +18.5% on LongMemEval at 90% lower latency. Graphs help a specific query class, not "memory" in general. | [5][8][10] |
| "Memory = long transcripts, retrieved top-k." | Store *distilled strategies* and curated facts; DC/ACE/OpenClaw all refuse transcripts. Transcripts are an audit log, not a context. | [15][18][19] |
| "Summarise the context regularly to keep it small." | Summarisation is where collapse happens: brevity bias + context collapse are named, measured failure modes. Fix = incremental delta bullets + counters + deterministic merge. | [19][20] |
| "Self-improvement requires RL / weight updates." | Language is a better medium than sparse rewards here: GEPA beats GRPO with up to 35× fewer rollouts; ACE self-adapts with *no labels*, from execution feedback. | [19][33] |
| "Agents can't touch their own code — that's research." | SICA edits its own harness (17%→53% on a SWE-bench Verified subset); DGM evolves an archive of coding agents (SWE-bench 20.0%→50.0%, Polyglot 14.2%→30.7%) and gets *better editing tools and long-context management* as emergent artefacts; AlphaEvolve/ADAS show the same loop for algorithm/architecture search. | [35][36][37][38] |
| "Skills/prompts are a nice-to-have, more is better." | Measured: +16.6 pp mean from curated skills, **≤3-module skills win**, small+skills ≈ larger model. Skills became a de-facto interchange standard (`SKILL.md`, agentskills.io) supported by Claude Code, Codex, pi, OpenClaw. | [21][22][23][24][50] |
| "Bigger context = better long-horizon behaviour." | Vending-Bench: derailment is uncorrelated with context exhaustion; long-horizon coherence is its own failure class. Compaction helps latency, not coherence. | [40][44] |
| "Sleep-time/offline reflection is a cute research demo." | Productised: Letta sleeptime agents, OpenClaw dreaming with promotion gates + review diary, memory-flush-before-compaction on by default. The research claim (5× less test-time compute) holds. | [13][14][15][51] |
| "Vendor memory benchmarks tell you what to buy." | Vendor-run benchmarks are now openly contested: Zep's published critique of Mem0's SOTA claim; an independent LoCoMo audit reporting ground-truth and scoring-framework errors; OpenAI abandoning SWE-bench Verified over flawed tests. Use them to compare *designs*, not to pick a stack. | [6][42][52][53] |
| "Self-improving ⇒ soon RSI ⇒ panic/utopia." | Careful measurement, no regime change: safety report treats automated AI R&D as a trend to monitor; Anthropic's own numbers put the remaining bottleneck at *judgement/goal-setting*, not code-writing. | [46][47] |
| "Your agent will honestly grade its own work." | In-band evaluation is a known distortion: models have altered behaviour when they inferred they were being evaluated/trained. Blind the grader, separate improver from eval, keep humans in the gate. | [45] |
| "Reflection is cheap, do it every turn." | Reflection belongs in a scheduled sweep; per-turn reflection taxes latency. Importance-threshold triggering (generative agents) and `curator_frequency` (ACE) both exist to make it rare and batched. | [3][20] |
| "Graph memory is mandatory for agents." | Graphs win on temporal/contradiction queries; markdown+frontmatter+`supersedes:` covers the coding-agent case at zero ops cost. Buy the graph when a real query fails without one. | [5][12][15] |

Newest signals worth tracking: paired A/B-style evaluation as the *only* credible way to report agent improvements [24][41]; skills as a versioned, installable package format with marketplaces (Claude Code plugin marketplaces for skill sets) [22]; provenance/taint discipline for memory as a security control [15]; agent-state serialisation as an open standard [51]; and prompt/procedure optimisation with LLM reflection as a distinct research area with its own optimisers (MIPROv2 → GEPA → ACE) rather than a hack [19][33][34].

## Minimal viable v1

One weekend-to-two-weekend of work. No server, no DB daemon, no embeddings at first.

```
.aiden/
  config.yaml            # budgets: memory_index_tokens: 2000, repo_map_tokens: 1000
  policy/self_write_allowlist.txt   # enforced by harness, not by prompt
  memory/
    episodes/2026-08-03.jsonl       # append-only; one record per task episode
    notes/                          # semantic, markdown + frontmatter (provenance, supersedes)
    MEMORY.md                       # curated index, hard token budget, always injected
  preferences/*.jsonl               # chosen/rejected pairs (context_ref pointers only)
  skills/<name>/SKILL.md            # Agent Skills format; candidates/ first
  dreams/DREAMS.md                  # consolidation diary — human review surface
  proposals/<ts>-<sha>/             # patch + rationale + eval report
evals/
  capability/*.yaml  regression/*.yaml   # split per [41]; deterministic graders
```

**Ship in this order** (each row is independently useful; stop anywhere):

| # | Piece | Mechanism | Source |
|---|---|---|---|
| 1 | Episode capture | on agent end: append `{task, files, tools, outcome, tests, tokens, user_diff, importance}` | [3][15] |
| 2 | `MEMORY.md` + budget | inject curated file; warn if truncated | [15] |
| 3 | Hybrid `memory_search` | sqlite FTS first, then embeddings; `score = recency·importance·relevance` | [3][10][15] |
| 4 | Pre-compaction flush | silent turn writing unsaved facts to `notes/` | [15] |
| 5 | Nightly consolidation | importance-thresholded reflection → merge/supersede/decay; diary to `DREAMS.md`; **never promote untrusted provenance** | [3][9][15] |
| 6 | Skill induction | recurring successful procedures → `candidates/SKILL.md` w/ `verified_by` | [16][17][24] |
| 7 | Eval harness | 15-25 tasks from your own real requests, deterministic graders, 3 seeds, paired runs | [41][42] |
| 8 | Self-update gate | proposal → static checks → worktree sandbox → paired eval → human approve → tag → canary → auto-revert | [37][41][45] |

v1 explicitly **does not**: use a graph store; fine-tune anything; let the agent edit `.pi/settings.json`, extensions, or `policy/**` without a human; auto-apply prompt changes; run self-updates mid-task; or delete memories.

Metrics from day one (a loop you cannot graph is a loop you cannot improve): episodes/day; % episodes with a captured user overwrite; `memory_search` hit rate and post-hit user correction rate; skill load rate + helpful/harmful ratio; `MEMORY.md` injected tokens vs budget; regression-suite pass rate per behaviour tag; self-update proposals: generated / passed gate / applied / rolled back; median tokens and turns per task (efficiency, the number that quietly degrades first).

## Implications for Aiden

**Copy, as-is:**
- CoALA's three-layer split, enforced by directory layout, not by discipline [4].
- Generative-agents retrieval scoring + *write-time* importance + threshold-triggered reflection [3].
- OpenClaw's file roles (`USER.md` / `MEMORY.md` / daily notes / `DREAMS.md`), pre-compaction memory flush, and dreaming's promotion gates incl. taint gating and `supersede-in-place` [15].
- Letta memory-block *shape* — `label/description/value/limit/read_only` — as the schema for Aiden's injected blocks; `description` is the mechanism the agent uses to decide how to write, and `read_only` is the right guardrail for shared/durable blocks [2].
- Aider's repo map: symbol-level map, graph-ranked into a token budget, re-ranked per chat state [39].
- ACE's delta-plus-counters-plus-deterministic-merge; never rewrite a whole learned context [19][20].
- Agent Skills format for all procedural memory + SkillsBench's ≤3-module rule [21][22][23][24].
- Anthropic's capability/regression eval split with graduation of capability evals [41].
- DGM's archive-of-variants + empirical validation, and SICA's discipline of editing only what the benchmark can measure [36][37].
- Pi's hook surface for the gate (`tool_call` blocking, project trust, `before_agent_start` for injection, `session_*` events for capture, `pi.appendEntry` for durable non-context state) — this is the whole point of orchestrating pi rather than writing a loop from scratch [50].

**Skip (and why):**
- Managed memory services (Letta/Zep/Mem0 as *dependencies*): their own benchmark disputes mean we cannot currently tell which is better, and we'd be learning their internals instead of ours [6][8].
- Graph databases in v1; multi-agent memory sync; embeddings for code identifiers; per-turn reflection; automatic prompt rewriting; weight updates; anything requiring a running service we have to operate.
- "Memory = conversation summariser." Explicitly rejected by the 2025-2026 evidence [18][19][40].

**Invent (our own risk, deliberately):**
- A *coding-agent-specific* eval suite for self-updates, where the oracle is the repo's own test suite — the piece none of the memory vendors has.
- Behaviour tags binding memory → prompt → skill → eval case, so a regression can be traced to the artifact that caused it (and to the episode that taught it).
- A human-visible "what Aiden learned today" digest in the TUI — review surface first, automation later. `DREAMS.md` in the terminal is the differentiator; the aesthetic TUI is not decoration, it is the gate.

## Open questions

1. **Does memory actually help coding agents, or just chat agents?** All the strong memory-benchmark evidence is conversational (DMR, LOCOMO, LongMemEval) [5][8][10]; there is no coding-agent equivalent, and the skeptic essays are unanswered [54]. Our paired eval is the honest way to find out — including the possibility that memory is neutral and only skills help [24].
2. **Optimal injection budget for `MEMORY.md`.** OpenClaw documents budgets and truncation but not the accuracy/cost curve [15]; ACE's 80k-token playbook is for a task-specific playbook, not durable cross-project memory [20]. Where is the knee for a personal harness?
3. **Skill count ceiling.** Pi/Claude Code put all skill descriptions in the system prompt; at 100+ skills that is ~3k tokens and a selection-quality problem. Retrieval-by-description degrades when descriptions are adjacent — need a hierarchy or a two-stage selector.
4. **Can helpful/harmful counters be trusted when the outcome signal is confounded?** Attribution of a failure to the loaded skill vs a bad task is unsolved; ACE's counters are per-bullet inside one run, not across months of heterogeneous personal use [19][20].
5. **Do we ever want weights?** SWE-RL/SEAL suggest signal exists in our own history [31][32], but at what data volume does LoRA on this machine beat a better skill, and who maintains the training loop? GEPA's "fewer rollouts" result keeps pushing the threshold further out [33].
6. **Self-modification governance.** Where exactly should the boundary sit once models complete longer tasks (METR's curve) [48] and the next-step judgement metric keeps climbing [46]? We want a policy that does not have to be rewritten every six months — probably "human owns goal-setting and the gate; agent owns everything inside the gate", restated per capability tier.
7. **Sleep-time predictability limit.** [13] ties benefit to query predictability. In a personal harness, what fraction of a day's work is predictable enough to precompute, and does the wasted compute cancel the latency win?
8. **Cross-harness identity.** Skills and `.af` give file-level portability [22][51]; memory and preferences do not have a standard yet, so a migration means re-consolidating from raw episodes. Is an open "memory file" standard worth proposing (or co-opting the agentskills effort for) later?

## Sources

1. MemGPT: Towards LLMs as Operating Systems — https://arxiv.org/abs/2310.08560
2. Letta docs, "What are memory blocks?" — https://docs.letta.com/v1-sdk/memory/memory-blocks/
3. Generative Agents: Interactive Simulacra of Human Behavior — https://arxiv.org/abs/2304.03442
4. Cognitive Architectures for Language Agents (CoALA) — https://arxiv.org/abs/2309.02427
5. Zep: A Temporal Knowledge Graph Architecture for Agent Memory (Graphiti) — https://arxiv.org/abs/2501.13956 · https://github.com/getzep/graphiti
6. Zep blog, "Lies, damn lies & statistics: is Mem0 really SOTA in agent memory?" — https://blog.getzep.com/lies-damn-lies-statistics-is-mem0-really-sota-in-agent-memory/
7. A-MEM: Agentic Memory for LLM Agents (NeurIPS 2025) — https://arxiv.org/abs/2502.12110
8. Mem0: Building Production-Ready AI Agents with Scalable Long-Term Memory — https://arxiv.org/abs/2504.19413
9. MemoryBank: Enhancing LLMs with Long-Term Memory (Ebbinghaus forgetting) — https://arxiv.org/abs/2305.10250
10. LongMemEval: Benchmarking Chat Assistants on Long-Term Interactive Memory — https://arxiv.org/abs/2410.10813
11. HippoRAG: Neurobiologically Inspired Long-Term Memory — https://arxiv.org/abs/2405.14831
12. From RAG to Memory: Non-Parametric Continual Learning (HippoRAG 2) — https://arxiv.org/abs/2502.14802
13. Sleep-time Compute: Beyond Inference Scaling at Test-time — https://arxiv.org/abs/2504.13171 · https://github.com/letta-ai/sleep-time-compute
14. Letta blog, "Sleep-time compute" — https://www.letta.com/blog/sleep-time-compute/
15. OpenClaw docs, "Memory overview" (USER.md/MEMORY.md/daily notes/DREAMS.md, dreaming, memory flush, taint gating) — https://docs.openclaw.ai/concepts/memory.md
16. Voyager: An Open-Ended Embodied Agent with LLMs — https://arxiv.org/abs/2305.16291 · https://voyager.minedojo.org/
17. Agent Workflow Memory — https://arxiv.org/abs/2409.07429
18. Dynamic Cheatsheet: Test-Time Learning with Adaptive Memory — https://arxiv.org/abs/2504.07952 · https://github.com/suzgunmirac/dynamic-cheatsheet
19. Agentic Context Engineering: Evolving Contexts for Self-Improving Language Models (ICLR 2026) — https://arxiv.org/abs/2510.04618
20. ACE reference implementation (generator/reflector/curator, config keys) — https://github.com/ace-agent/ace · https://ace-agent.github.io/
21. Anthropic Engineering, "Equipping agents for the real world with Agent Skills" — https://www.anthropic.com/engineering/equipping-agents-for-the-real-world-with-agent-skills
22. anthropics/skills (SKILL.md template, spec, Claude Code plugin marketplace) — https://github.com/anthropics/skills
23. Agent Skills specification — https://agentskills.io/specification
24. SkillsBench: Benchmarking How Well Agent Skills Work Across Diverse Tasks — https://arxiv.org/abs/2602.12670 · https://skillsbench.ai/
25. Reflexion: Language Agents with Verbal Reinforcement Learning — https://arxiv.org/abs/2303.11366
26. Self-Refine: Iterative Refinement with Self-Feedback — https://arxiv.org/abs/2303.17651
27. CRITIC: LLM Agents Can Self-Correct with Tool-Interactive Critiquing — https://arxiv.org/abs/2305.11738
28. Direct Preference Optimization: Your Language Model is Secretly a Reward Model — https://arxiv.org/abs/2305.18290
29. Constitutional AI: Harmlessness from AI Feedback — https://arxiv.org/abs/2212.08073
30. Self-Rewarding Language Models — https://arxiv.org/abs/2401.10020
31. SEAL: Self-Adapting Language Models — https://arxiv.org/abs/2506.10943 · https://jyopari.github.io/posts/seal
32. SWE-RL: Advancing LLM Reasoning via RL on Open Software Evolution — https://arxiv.org/abs/2502.18449 · https://github.com/facebookresearch/SWE-RL
33. GEPA: Reflective Prompt Evolution Can Outperform Reinforcement Learning (ICLR 2026) — https://arxiv.org/abs/2507.19457 · https://github.com/gepa-ai/gepa
34. Optimizing Instructions and Demonstrations for Multi-Stage LM Programs (MIPROv2/DSPy) — https://arxiv.org/abs/2406.11695
35. Automated Design of Agentic Systems (ADAS) — https://arxiv.org/abs/2408.08435
36. A Self-Improving Coding Agent (SICA) — https://arxiv.org/abs/2504.15228
37. Darwin Gödel Machine: Open-Ended Evolution of Self-Improving Agents — https://arxiv.org/abs/2505.22954
38. AlphaEvolve: a Gemini-powered coding agent for designing advanced algorithms — https://deepmind.google/blog/alphaevolve-a-gemini-powered-coding-agent-for-designing-advanced-algorithms/ · https://arxiv.org/abs/2506.13131
39. Aider docs, "Repository map" (graph ranking, `--map-tokens`) — https://aider.chat/docs/repomap.html · impl: https://github.com/Aider-AI/aider/blob/main/aider/repomap.py
40. Anthropic Engineering, "Effective context engineering for AI agents" — https://www.anthropic.com/engineering/effective-context-engineering-for-ai-agents
41. Anthropic Engineering, "Demystifying evals for AI agents" (Jan 2026) — https://www.anthropic.com/engineering/demystifying-evals-for-ai-agents
42. OpenAI, "Why we no longer evaluate SWE-bench Verified" — https://openai.com/index/why-we-no-longer-evaluate-swe-bench-verified/
43. Claude Code docs, "How Claude remembers your project" (CLAUDE.md + auto memory dir) — https://code.claude.com/docs/en/memory
44. Vending-Bench: A Benchmark for Long-Term Coherence of Autonomous Agents — https://arxiv.org/abs/2502.15840 · https://andonlabs.com/evals/vending-bench
45. Anthropic, "Alignment faking revisited" — https://alignment.anthropic.com/2025/alignment-faking-revisited/ · original: https://arxiv.org/abs/2412.16339
46. Anthropic Institute, "When AI builds itself" (recursive self-improvement; evidence from within Anthropic) — https://www.anthropic.com/institute/recursive-self-improvement
47. International AI Safety Report 2026 — https://internationalaisafetyreport.org/publication/international-ai-safety-report-2026
48. METR, "Measuring AI Ability to Complete Long Software Tasks" — https://metr.org/blog/2025-03-19-measuring-ai-ability-to-complete-long-tasks/ · https://metr.org/time-horizons/
49. Pi docs, "Security" (project trust is an input guard; no built-in sandbox) — local: `docs/security.md` in the installed `@earendil-works/pi-coding-agent` package
50. Pi docs, "Extensions" and "Skills" (lifecycle events, `tool_call` blocking, skill locations, progressive disclosure) — local: `docs/extensions.md`, `docs/skills.md`
51. Letta Agent File (`.af`): open format for serialising stateful agents — https://github.com/letta-ai/agent-file · https://www.letta.com/blog/agent-file/
52. Independent LoCoMo audit (ground-truth and scoring errors) — https://github.com/dial481/locomo-audit
53. LoCoMo: Evaluating Very Long-Term Conversational Memory of LLM Agents — https://github.com/snap-research/locomo
54. Local precedent skills in this environment — `~/.agents/skills/session-recap/SKILL.md`, `~/.agents/skills/cavecrew/SKILL.md`. Skeptic counterpoint on agent memory for coding: "Coding agents do not need personal memory" — https://softwareguru.substack.com/p/coding-agents-do-not-need-personal
