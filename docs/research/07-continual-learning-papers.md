# Continual Learning Papers — Implementation Notes

Reading notes on the four papers that define what "continual learning" should mean for Aiden,
written against the *mechanism* level (what we would actually code), not the summary level.
Companion to [00-reference-source-study.md](00-reference-source-study.md) (Prime Agent ships
Continual-Harness ideas as code) and [03-continual-learning.md](03-continual-learning.md).

## TL;DR

- **The core idea is a two-loop architecture**: an *inner* loop that acts, and an *outer* loop
  that edits the harness state `H = (prompt p, sub-agents G, skills K, memory M)` from a
  trajectory window. Nothing else in the literature is new; this is the shape. [1]
- **Reset-free is the whole point.** GEPA-style prompt optimisation needs full episodes and
  resets. Continual Harness edits *mid-episode*, so failure signatures observed early stay
  available to every later refinement — "refinement quality compounds with episode length". [1]
  For a coding agent, a "reset" is a fresh session; so the design consequence is: *learning must
  be able to happen inside one long working session, not only at session end.*
- **Capability floor is real and brutal.** On Pokémon Emerald, harness refinement is strictly
  Pareto-dominant on Gemini-3-Pro (100% milestones at $130 vs 98% at $215 — ~40% cheaper),
  *high-variance* on Flash, and **worse than doing nothing** on Flash-Lite (33–13% vs 20%). [1]
  Aiden's continual learning will not rescue a weak model. Measure per-model, never globally.
- **Refinement concentrates.** In the GPP run, CRUD ops over a whole episode hit a *small subset*
  of components; the battle-strategist prompt cycled grow→simplify→structural-rewrite. Learning
  is iterative rewriting of a few artifacts, not steady accretion. [1]
- **Skills can be measured against an oracle, and they self-improve toward it.** Navigation
  skills evolved in-loop reduced path-cost deficit vs a Dijkstra oracle from ~50% to single
  digits within a 24h run, with hundreds of invocations. This is the template for Aiden: *every
  learned skill needs an oracle we can score against.* [1]
- **RLM is the context-half of the same story**: treat the prompt as a variable in a persistent
  REPL, delegate to sub-LLMs, never summarize. Prime Intellect's blog is the ablation record;
  the paper is the method. Median lift vs compaction 26%, vs CodeAct-with-subcalls 130%, vs
  Claude Code 13% (GPT-5 on their long-context suite). [2][3]
- **ACE gives the write-policy**: grow-with-refine + targeted edits via an incremental
  delta ("playback"), and a *fast/slow* split between the generator that uses the context and
  the reflector/curator that evolves it. It also warns of **context collapse** — a long,
  self-referential context that has to be rewritten shrinks drastically and loses accuracy. [4]
- **Sleep-time compute** justifies the idle-time half: pre-compute context offline so latency
  and cost shift to idle time. Directly applicable to Aiden's "when nothing is running,
  consolidate". [5]

## 1. Continual Harness (arXiv:2605.09998)

**Formalism.** Harness `H` mediates behaviour through 4 components: system prompt `p`,
sub-agents `G`, skills `K`, memory `M`. The harness additionally exposes **meta-tools**
(`define_agent`, `run_code`, `process_memory`, …) through which *the agent itself* edits
`p,G,K,M` in place. Three regimes:

| Regime | Definition |
|---|---|
| `H_min` | env interface + generic prompt; no sub-agents/memory/skills |
| `H_expert` | all components hand-engineered (PokeAgent: A\* pathfinding, type chart, damage calc, curated objectives) |
| `H_CH` | starts at `H_min`; a Refiner rewrites `p,G,K,M` during the episode |
| meta-harness (GPP later runs) | model given only meta-tools; it *built its own* specialists unprompted |

**Two loops.**

```
inner (every step t):   a_t ~ M(s_t, H_t, τ_≤t)          s_t = (frame o_t, text map m_t)
outer (every F steps,   Refiner reads τ_{t-F:t}, detects failure signatures,
       after warm-up W):  emits Δ = (Δp, ΔG, ΔK, ΔM) via the same meta-tool API
                          H_{t+1} = H_t ⊕ Δ       # no reset; enters context next step
```

Agent and Refiner **share the same model**; they differ only in *when invoked* and *on what
trajectory context*. That is the key implementation simplification for Aiden: the refiner is a
role, not a subsystem.

**The four refinement passes** (§3.2), in order, each scoped to one component:

1. `p` — rewrite prompt conditioned on identified failures + window.
2. `G` — **create** sub-agent entries for repeated multi-step patterns; **edit** entries whose
   failures were detected; **delete** entries "not invoked productively".
3. `K` — **codify** skills from successful sequences; **repair** executable code that raised.
4. `M` — **add** to fill gaps; **update** stale; **demote importance** for what the agent has
   moved past.

Failure signatures it looks for are enumerated: *navigation loops, tool-call failures, stalled
objectives, missed exploration opportunities.*

**Delete and demote are first-class.** Most "agent memory" designs only ever add. Here, deletion
is explicitly tied to non-use, and importance decay is tied to irrelevance. Aiden must implement
both or the store rots.

**Co-learning loop (model weights, §3.3, §4.5).** After SFT warm-up on frontier trajectories and
an offline GRPO stage on a per-step process reward (neither alone produces milestone progress):

```
for k in iterations:
    rollout π_{θk} inside live-refining H_t for K=256 steps      # batch size 1
    pairwise PRM R(s_t, a_t, τ) ∈ [0,1] scores each transition over a sliding window
    low-reward windows → relabeled by frontier teacher (Gemini-3.1-pro)
    soft SFT on the relabeled shard → θ_{k+1}
    # reset-free: emulator state at end of k is the start of k+1
```

`D_{θ}` depends on `θ` *through the harness*: actions → trajectory → refinement → next
observation distribution. Harness state updates *within* an iteration, weights update *across*
iterations. Same trajectory data feeds both loops.

**Results that matter for us.**

| Claim | Evidence |
|---|---|
| Refinement recovers most of the `H_min`→`H_expert` gap | milestones-vs-button-press curves on Red & Emerald; residual gap only in dialogue-heavy gyms + multi-turn battle strategy |
| Compounding across episodes | bootstrap-updating beats from-scratch at *every* milestone on Red; bootstrap-frozen (inherit, no refine) matches-or-loses to updating → continued refinement adds value on top of inheritance |
| Capability floor | Emerald Pareto: Pro dominant (100% @ $130 vs 98% @ $215); Flash high-variance (80% @ $42 vs 77% @ $30); Flash-Lite *worse* than `H_min` |
| Skill self-improvement is measurable | path-cost deficit vs Dijkstra oracle: ~50% → single digits in-run; `H_min` invokes 0 navigation skills, `H_CH` hundreds/24h |
| Not saturated | "did not establish a convergence point" |

**Stated limits.** No teacher/trainee unification for open-source ≤31B; head-to-head
reset-free vs batch-with-resets left open; gains are component-specific and unreliable for
dialogue/multi-turn planning.

## 2. RLM (arXiv:2512.24601) + Prime Intellect's ablations

**Method.** Treat a long prompt as **part of the external environment**: the model programmatically
examines, decomposes, and recursively calls itself over *snippets* of it. Handles inputs up to
**two orders of magnitude beyond** the context window. [2]

Headline quality numbers (median across evaluated benchmarks, GPT-5): **+26% vs compaction,
+130% vs CodeAct with sub-calls, +13% vs Claude Code**, at comparable cost. Post-training a
model *around* the scaffold: RLM-Qwen3-8B beats Qwen3-8B by **+28.3%** average and approaches
vanilla GPT-5 on 3 long-context tasks. [2] → the scaffold is trainable, which is the Bitter-Lesson
argument: don't hand-engineer memory hierarchies, train the policy that manages context.

**Prime's implementation choices** (verifiers flavour) worth copying exactly [3]:

- **Tools live behind sub-LLMs.** Anything token-hungry (web `open`, file reads) is only callable
  by a child, so the parent never sees the dump. "Many tools produce a lot of tokens… the main
  RLM doesn't have to see those tokens."
- `llm_batch(prompts)` → parallel sub-LLM calls, and the model is *told* the per-call timeout and
  elapsed time so it can reason about its own scheduling.
- **Answer-as-mutable-variable**: `answer = {"content": "", "ready": False}`; only
  `ready=True` ends the rollout. Final answers get *edited* across turns instead of streamed once
  — the blog explicitly calls this "a form of diffusion over the reasoning chain".
- **REPL stdout is capped (default 8192 chars, adjustable)** — the forcing function that makes the
  model use Python and children instead of pasting the corpus into its own context.
- Per-REPL-call timeout 120s default.

**Negative results, which we should believe [3]:**

| Env | What happened |
|---|---|
| `math-python` | RLM **worse** than plain Python-tool LLM. Same capability available, strictly worse outcome → evidence of benchmark overfit + scaffolding tax; "a model properly trained in the RLM should at least match" |
| `DeepDive` | RLM worse **unless** given an explicit `<env_tips>` decomposition strategy → performance is left untapped by poor scaffold *usage* |
| `Oolong` *real* subset | large RLM win (~1.5M chars ≈ 300–400k tokens); on *synth* the plain LLM wins; `synth-with-labels`: RLM never used sub-LLMs, solved with regex, perfect |
| `verbatim-copy` | RLM better, but very different strategy (iterate + `str.replace` fixes) and more tokens |
| timing | **worse everywhere** |

Also: sub-LLM calls are completion-token-heavy with tiny prompts ⇒ "scaling thinking tokens at
low main-context", and the authors flag that GPT-5-mini often fails to parallelize ⇒ training
headroom.

**Design consequence for Aiden.** Adopt RLM *as a context-discipline*, not necessarily as
"one REPL tool": cap tool output, never let token-hungry fetches land in the parent context,
delegate verbose work to children with a *minimum* context, and let the answer be an edited
artifact. Keep the full kernel-as-only-tool design as a v2 experiment with a private eval set,
because the negative results are real.

## 3. ACE — Agentic Context Engineering (arXiv:2510.04618)

Three-agent split: **Generator** (uses current context to produce a rollout) → **Reflector**
(extracts lessons from the generation and from the state of the context itself) → **Curator**
(applies structured updates to the context). Two properties that the paper says must hold
together, and that Aiden inherits:

- **Grow-with-refine**: the context is allowed to scale up with experience rather than being
  rewritten flat each time.
- **Balance-in-the-loop**: keep *incremental* delta updates (a "playback" of what changed) rather
  than monolithic rewrites.

**Context collapse** is the failure mode to design against: an agent that repeatedly rewrites a
long, self-referential context can *drastically shrink and lose accuracy* in one step. [4]
Practical rule for Aiden: **never let the refiner emit a whole replacement body.** Only
`create/update/delete` against itemised entries — exactly the operator set Prime's `/refine`
exposes, and exactly why its prompt says "small evidence-backed edits".

Reported wins are in-domain (agent and doc-analysis benchmarks; e.g. large AppWorld gains,
long-context improvement over baselines in the paper's abstract) — treat magnitudes as
setting-specific, direction as reliable. [4]

## 4. Sleep-time compute (arXiv:2504.13171)

Allows a model to "think" while idle, pre-computing features/intuitions/answers before the next
query, improving accuracy-optimally and **shifting latency and inference cost to idle time**. [5]
For Aiden: the natural host for *offline* consolidation — merge duplicate memories, prune
non-invoked skills, re-score importance, pre-summarise recently-touched files — triggered on
session end / idle, never blocking the user's turn. Prime Agent's daemon + heartbeat machinery is
the infrastructure version of this idea.

## 5. Supporting lineage (for citations, not re-explaining)

- **Voyager** [6] — the original growing skill library of *executable code* + retrieval on
  task-embedding; automatic curriculum; iterative prompt refinement from environment feedback +
  self-verification. Aiden's skill entry schema is this plus versioning.
- **Agent Workflow Memory** [7] — induces reusable *workflows* from successful trajectories
  online and injects them into the agent, improving long-horizon web tasks; the "codify repeated
  multi-step patterns → G/K" pass in Continual Harness is its descendant.
- **Reflexion** [8] / **Self-Refine** [9] — verbal self-feedback stored in episodic memory vs
  iteratively revised output. These are the *inner* reflection primitives; neither maintains
  durable harness state, which is the gap Continual Harness closes.
- **MemGPT** [10] — OS-style paged memory with explicit function calls to move data between
  tiers; the origin of "the model manages its own context via calls", i.e. RLM's ancestor.
- **Generative Agents** [11] — memory stream + retrieval scored by
  recency × importance × relevance, and periodic *reflection* nodes that synthesise higher-level
  observations. That retrieval triple is still the right cheap default.
- **Meta-Harness** (arXiv:2603.28052) [12] — end-to-end optimisation of the *whole* harness,
  cited as concurrent work; watch it as the formal-optimization counterpart.
- **PokeAgent Challenge** (arXiv:2603.15563) [13] — where `H_expert` and the milestone/
  button-press metric come from; a benchmark for coding agents is *not* the same thing, but the
  "oracle cost per milestone" metric design transfers.

## 6. Implications for Aiden — the design this forces

1. **Harness state is a first-class artifact with 4 kinds**, `prompt | memory | skill | subagent`,
   versioned, with `scope ∈ {local, global}`, `reference`, `arguments`, `metadata`, and an
   append-only `refinement` event log. (Prime already proved the schema works; the paper proves
   the loop works.)
2. **Refine inside a session, not only at session boundaries.** Trigger cadence = every `F`
   turns/steps after a warm-up of `W`, plus explicit user `/refine`. Cheap gate-model call
   decides `shouldRefine`.
3. **Operators are CRUD-only + mandatory `evidence` + `expectedOutcome`.** No full-body rewrites
   (ACE's context collapse). Base system prompt immutable — learning is *supplemental*.
4. **Deletion and demotion are required**, keyed on "not invoked productively" and staleness.
5. **Everything learned is scored.** Each kind gets an oracle: skills → a deterministic checker
   (tests, path-cost-deficit analogue); memory/prompt → the private eval set; subagents →
   cost+outcome of delegated work. Unscored learning is accumulation, not learning.
6. **Per-model gates.** Because the capability floor is sharp, enable refinement per model:
   strong model → full loop; weak model → `H_min`-equivalent. Store the gate with the model, not
   globally.
7. **Idle-time consolidation** (sleep-time compute): merge/prune/re-score off the critical path.
8. **Cost attribution per turn including children** from day one, or none of the above is
   measurable — and Prime's `child_usage_attributed` + subtract-in-tree reporting is the pattern.
9. **Self-update boundary.** The Refiner edits harness state, *never* source files ("Never edit
   source files directly" is a literal line in Prime's refine prompt). Aiden adds a second tier:
   code/tool self-modification goes through a git branch + eval gate + human approve, and is
   *outside* what the refiner can do.

## Open questions

- What `F` (refinement cadence) and `W` (warm-up) are for a *coding* episode? Paper values are for
  24h gameplay runs; a coding session is 20 turns to 2000 tool calls. Needs measurement.
- Continual Harness deletes sub-agents "not invoked productively" — how is *productively* decided?
  Paper doesn't specify a threshold. Candidate: invoked ≥N times AND never appeared in a
  low-PRM window.
- No public code for the Pokémon-side Refiner (only Prime Agent's `/refine`, which is coding-domain
  and different in detail). We re-implement from the paper's four passes.
- Does refinement ever help *against* a stronger baseline harness in coding (where `H_min` is
  already good)? Coding harnesses start far above `H_min`, so the measured gap may be much
  smaller — likely the single biggest unknown in Aiden's premise.

## Sources

- [1] S. Karten et al., *Continual Harness: Online Adaptation for Self-Improving Foundation
  Agents*, arXiv:2605.09998 (v1 2026-05-11), https://arxiv.org/abs/2605.09998 ,
  HTML https://arxiv.org/html/2605.09998v1 , project page https://sethkarten.ai/continual-harness
- [2] A. Zhang et al., *Recursive Language Models*, arXiv:2512.24601 (v3 2026-05-11),
  https://arxiv.org/abs/2512.24601 , code https://github.com/alexzhang13/rlm ,
  original post https://alexzhang13.github.io/blog/2025/rlm/
- [3] Prime Intellect, *Recursive Language Models: the paradigm of 2026*,
  https://www.primeintellect.ai/blog/rlm (experiments on
  https://github.com/PrimeIntellect-ai/verifiers branch `sebastian/experiment/rlm`)
- [4] *Agentic Context Engineering: Evolving Contexts for Self-Improving Language Models*,
  arXiv:2510.04618, https://arxiv.org/abs/2510.04618
- [5] *Sleep-time Compute: Beyond Inference Scaling at Test-time*, arXiv:2504.13171,
  https://arxiv.org/abs/2504.13171
- [6] *Voyager: An Open-Ended Embodied Agent with Large Language Models*, arXiv:2305.16291,
  https://arxiv.org/abs/2305.16291
- [7] *Agent Workflow Memory*, arXiv:2409.07429, https://arxiv.org/abs/2409.07429
- [8] *Reflexion: Language Agents with Verbal Reinforcement Learning*, arXiv:2303.11366
- [9] *Self-Refine: Iterative Refinement with Self-Feedback*, arXiv:2303.17651
- [10] *MemGPT: Towards LLMs as Operating Systems*, arXiv:2310.08560
- [11] *Generative Agents: Interactive Simulacra of Human Behavior*, arXiv:2304.03442
- [12] Y. Lee et al., *Meta-Harness: End-to-End Optimization of Model Harnesses*,
  arXiv:2603.28052, https://arxiv.org/abs/2603.28052
- [13] S. Karten et al., *The PokeAgent Challenge*, arXiv:2603.15563,
  https://arxiv.org/abs/2603.15563
- [14] Prime Intellect, *Prime Agent* README + `packages/coding-agent/docs/rlm.md`,
  `rlm-runtime.md`, `refinement.ts`, `prime-agent-runtime/src/rlm/harness.py`,
  https://github.com/PrimeIntellect-ai/prime-agent
- [15] Context-rot background: https://research.trychroma.com/context-rot ;
  long-context performance decay: https://nrehiew.github.io/blog/long_context/
