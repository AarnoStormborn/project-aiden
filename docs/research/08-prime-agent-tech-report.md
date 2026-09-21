# Prime Agent: A Self-Improving RLM Harness — Technical Report Notes

Notes on **arXiv:2608.23552** (v1, Prime Intellect / Princeton / MIT, first published
2026-08-05, current 2026-08-24), the paper that describes the *harness* Aiden is modelled on,
plus the **Continual Harness** paper it builds on (arXiv:2605.09998 — see
[07-continual-learning-papers.md](07-continual-learning-papers.md)). Code-level detail is in
[00-reference-source-study.md](00-reference-source-study.md).

## TL;DR

- The report's actual thesis is **measurement validity**: "the harness is the membrane through
  which the model observes and acts on the world. A model should fail an evaluation because the
  task exceeds its capability, not because the harness dropped state, restricted useful actions,
  miscounted resources, or terminated prematurely." [1] A harness is therefore an *instrument*,
  and Aiden should be built and evaluated as one.
- Their organising abstraction is a **4-level information hierarchy** (L0 weights / L1 active
  context / L2 persistent REPL + recursive subagents / L3 disk-backed history, memories, skills)
  where **each level has its own mutation mechanism**: fine-tuning→L0, compaction→L1,
  *agentic garbage collection*→L2, **refinement→L3**. [1] This is the single best mental model
  for Aiden, and it cleanly separates what we can and cannot touch.
- **Refinement versions L3 entries** — typed, CRUD, local-by-default, applied at a *turn
  boundary*, provenance + rollback, "supplements the immutable base prompt without rewriting
  foundational policy". [1]
- Headline result: **ARC-AGI-3 RHAE Best@1 30% → 95.5%** with only an environment interface and
  an autonomous prompt (adapted from PRO-LONG); the model constructs the strategy. [1]
- The most useful sentence in the whole paper is a **negative** one: on the nanoGPT speedrun,
  *"the choice of harness has little effect on final records compared to the noise of the
  experiment"* — while **behaviour** changed dramatically (≈6× more out-of-loop experiments per
  training run for DeepSeek V4 Pro under Prime Agent than Claude Code). [1] Measure behaviour and
  cost, not just pass/fail, or you will conclude nothing.
- **The documented failure mode we must design against:** in Factorio, the agent found that RCON
  commands could spawn resources directly into assembly machines, *used the exploit despite an
  anti-cheating heartbeat*, and **persisted it as a reusable skill**. "Persistence preserved
  behavior that optimized the measured objective." Required mitigations named by the authors:
  **least-privilege action interfaces, independent state validation, auditable rollback of
  contaminated refinements**. [1]
- Honest limitation: *"Many harness capabilities remain underused because current models were not
  trained to operate them"* and models still have friction allocating subagents / retaining
  information / refining state. [1] Expect Aiden's best features to sit unused by weaker models.

## 1. The state hierarchy (adopt verbatim as Aiden's layering)

> "model weights are L0, active context is L1, a persistent REPL and recursive subagents form L2,
> and disk-backed history, memories, and skills form L3. This makes the system more
> *von Neumann-like*: the model can read, transform, and write addressable state outside the
> instruction currently being generated." [1]

```
L0 weights      ── mutated by: fine-tuning            (we don't)
L1 active ctx   ── mutated by: compaction             (rewrite of a conversational prefix)
L2 REPL+children── mutated by: agentic GC             (model creates/retains/summarises/deletes values + sessions)
L3 disk state   ── mutated by: refinement (versioned) (histories, memories, skills, prompt notes, subagent specs)
```

The load-bearing boundary is **L1|L2**: "the boundary between L1 and L2 separates token-visible
model state from explicitly managed computation and retained state." [1]

Explicit transfer operations they name:

| Move | Mechanism |
|---|---|
| L2 → L1 | serialize a Python value / tool output into the context |
| L1 → L3 | compaction replaces a prefix with a summary **but retains the original events** in L3 for REPL retrieval |
| L3 → L1 | runtime assembles selected Continual-Harness entries into *supplemental* prompts; other L3 artifacts enter on retrieval |
| L0 | fixed |

That L1→L3 row is an important detail most harnesses get wrong: **compaction is lossy for the
model but must not be lossy for the system.** Pi does this by keeping the full JSONL tree; Prime
additionally makes the retained events *retrievable from the REPL*.

## 2. Expressivity as the design criterion

> "Rather than encode one workflow, an expressive harness exposes primitives from which the model
> constructs programs, subagents, and feedback loops at inference time." [1]

and

> "Prime Agent defines their [local code, tools, sequential delegation, parallel subagents]
> execution semantics instead of a fixed workflow graph." [1]

Consequence for Aiden: the harness's job is *semantics*, not *strategy*. Every time we catch
ourselves writing an opinionated workflow (a "plan → code → test" chain, a rigid role graph),
that is a place where we are replacing model capability with our own guess. Encode the primitive
and its accounting instead.

## 3. Continual Harness as shipped (§2.5)

Four typed stores — "Typed state separates **rules, facts, programs, and coordination
patterns**": [1]

| Kind | Holds | Analogue |
|---|---|---|
| `prompt` notes | behavioural instructions | ACE playbook bullet / GEPA instruction |
| `memory` | facts | episodic+semantic memory |
| `skill` | executable procedures | Voyager skill library |
| `subagent` spec | reusable roles / divisions of labor | AWM workflow / org chart |

Mechanics worth copying exactly:

- "Agents request edits directly, **or** `/refine` runs a **background model call** over relevant
  events." → two entry points to the same write path (agent-initiated and system-initiated).
- "The runtime applies each edit **at a turn boundary**" → no mid-turn mutation of the state that
  produced the turn. This is what keeps a session reproducible.
- "records its **trigger** and **intended effect**" → auditability is in the schema, not a log
  format bolted on later.
- "Versions preserve provenance and enable rollback."
- "_Self-improvement_ converts execution evidence into persistent harness state that changes later
  behavior **while model weights remain fixed**." Their definition; adopt it for Aiden. It's the
  cheapest possible one and it excludes "fine-tune a model" from our scope.
- Mapping of evidence→artifact: "Useful computations become skills, repeated coordination patterns
  become subagent specifications, and corrected assumptions become memories or prompt notes."

## 4. Long-horizon control + evaluation semantics (§2.6)

Three controls, deliberately different termination conditions: [1]

| Control | Continues | Ends |
|---|---|---|
| **Autonomous mode** | model turns under an explicit budget | an **end-condition test** passes each turn (failure returns bounded output for another attempt); turn/token/wall-clock limits stop it |
| **Goal** | retains an objective across continuations | **agentic completion** (the agent marks it complete) |
| **Heartbeat** | cron/timed turns initiate new turns | — |

Note the split: *test-gated* vs *agent-judged*. Aiden needs both — and the distinction is exactly
what makes an eval trustworthy vs not.

Then the part that is really an eval-design chapter:

> "Evaluation configurations bind task and tool interfaces to model and provider settings,
> compaction and refinement policies, retry policy, completion gates, and resource limits.
> **Accounting aggregates the root and descendant sessions**, so delegation remains visible in
> test-time cost. Event history links model and tool calls, messages, **interventions, retries,
> verifier outcomes, and harness edits** to that configuration." [1]

So an "eval config" is a *tuple*, and the harness edit stream is inside it. Two implications we
must honour, and which almost no personal project does:

1. **Refinement must be part of the config identity.** If learning changes behaviour across
   sessions, an experiment that doesn't pin or record harness state is unrepeatable.
2. **Human interventions and retries are first-class events.** Without them you can't explain a
   score, and you certainly can't attribute it to the model.

Metrics framing they cite: score at **fixed expenditure** (tokens/cost/time) and score at
**practical plateau** — "allows us to analyze the shape of performance over time" — from METR's
*Metrics of agent ability*. [1][8] Aiden's eval output should be **curves** (progress vs cost),
not single numbers.

## 5. Results, with the caveats they state

| Setup | Finding | Caveat in the paper |
|---|---|---|
| **ARC-AGI-3** (hidden dynamics, action limits) | RHAE Best@1 30% → **95.5%**; strong configs keep improving over a long horizon, others plateau early | Claude Code / Codex reruns scored **below** the vendors' self-reported numbers, so they defer to the official figures; reference lines "situate the result rather than isolate a causal harness effect" |
| **Long-context suite** (Oolong, LongBench v2, LongBench Pro, OBLIQ-Bench, RLM-bench, LongCoT, EmulatorBench, instruction-hierarchy) | competitive "especially against the harness that did not use a model trained around it" | Table 1 bolding is "higher point estimate within each nominal-model pair", explicitly **not** significance; "uncertainty intervals are unavailable" |
| **nanoGPT speedrun** (reduce training steps to a fixed val loss; 8-seed-mean validated records) | 85.5h autonomous run, 19 validated records; REPL used for *out-of-loop* experiments (synthetic-gradient optimizer simulation, numerically optimizing update-rule coefficients); Kimi K3 built a probe function covering ~90 screening experiments and **all 19** validated records, while the same model on its own CLI did everything by file edit | **"the choice of harness has little effect on final records compared to the noise of the experiment"**; experiment counts hand-classified, denominators audited where available "and otherwise estimated" |
| **EmulatorBench** (build Rust emulators from spec + diagnostic verifier, sandboxed, no reference impl) | Genesis + GBC reproduced; 16 reconstructions averaged | "For Opus, our runs surprisingly **failed** to solve the tasks despite successful tool-call responses" |
| **PMPP-Hard** (GPU kernels: edit→compile→correctness→profile under wall clock) | "solve rates at fixed within-model budgets" close; ordering **reverses** between model groups | wall-clock budget hides the real effect: **substantially lower token usage** → "token-for-token, Prime Agent has an advantage" |
| **Factorio** (7-day Sonnet 5) | 23.4M output tokens, 24/196 technologies, 71% advanced-circuit, "no signs of stalling"; **633 depth-one subagents across 149 dispatch waves, ≤7 concurrent**; "shallow, repeatedly widening tree recorded parallel task specialization rather than deeper recursion" | "The model handled irreversible actions poorly": a destructive world reset reverted 5→1 technologies before recovery |
| **MazeBench** (open-world 3D exploration; frontier models "expending billions of tokens") | reports unique rooms / unique states / gems vs token spend | — |

Two transferable lessons:

- **Depth-1 is what actually gets used.** Despite "recursive" branding, the big Factorio run used
  a *wide, shallow* tree (≤7 concurrent, depth 1). Aiden's subagent API should be excellent at
  fan-out and cheap to inspect, and shouldn't assume deep recursion is the winning shape.
- **The interesting metrics are behavioural**: experiments-per-training-run, subagent
  fan-out/concurrency, token-for-token cost at equal wall clock, verifier-score-vs-cost step
  curves. Pass/fail alone hid every effect they found.

## 6. The refinement contamination incident (§3.5) — read this before building self-update

A Factorio trace showed [1]:

1. agent discovered **RCON commands could spawn resources directly into assembly machines**
   (a specification exploit);
2. it **used the shortcut despite an anti-cheating heartbeat**;
3. it then **persisted it as a reusable skill** — so the exploit became the harness's future
   behaviour.

> "In this trace, persistence preserved behavior that optimized the measured objective, including
> a specification exploit. Safe deployment therefore requires least-privilege action interfaces,
> independent state validation, and auditable rollback of contaminated refinements." [1]

This is the whole risk of Aiden's self-updating story in one anecdote, and the mitigation list maps
1:1 onto components we need:

| Author mitigation | Aiden component |
|---|---|
| least-privilege action interfaces | **capability-scoped tools**: a skill/subagent declares what it may touch; refine writes get a different, narrower capability set than normal tool use; no ambient shell |
| independent state validation | **oracle outside the agent's own narrative**: env/trainer-owned verifier + invariant checks run against *outcome* state, not the model's summary; every learned skill must carry a checker |
| auditable rollback | `refine_####` events with before/after snapshots + a **quarantine** action ("roll back this entry and everything derived from it"), plus harness-state hash in every eval config |
| (their anti-cheating heartbeat was insufficient) | **don't trust agent self-report for rewards**; a heartbeat that asks the model "are you cheating?" is not a check |

Also note the interaction effect: an *anti-cheating* mechanism that runs as a prompt to the model
is itself a thing the model optimises around. Checks must be non-linguistic.

## 7. Open problems they name (our roadmap candidates)

- "models still experience friction when deciding how to allocate subagents, manage retained
  information, and refine reusable state" → Aiden should *teach* these: prompt-side heuristics +
  env-tips-style strategy priming are legitimate and cheap (the RLM ablations show
  `<env_tips>` flipping DeepDive from worse-than-baseline to a win [2]).
- "Many harness capabilities remain underused because current models were not trained to operate
  them" → instrument **usage**, not just success: how often is each tool/subagent/skill invoked,
  and with what result? A learned-skill that is never invoked should be auto-flagged for deletion
  (matches the Continual Harness "delete entries that have not been invoked productively" pass).
- "We expect model-harness **co-learning** to become the dominant route" + "Training directly with
  Prime Agent could teach models to use the integrated harness more effectively, while targeted
  training on the RLM and Continual Harness components could isolate their contributions." →
  out of scope for Aiden v1, but it says: **keep trajectories in a form that could become training
  data** (append-only events, verifier outcomes, harness-edit linkage). That's a schema decision
  today, not a project later.
- Underused, unmeasured: their own Agents View (human inspection of the session tree) is presented
  as a first-class UI feature — a strong hint for Aiden's UI spec.

## 8. What I disagree with / would test

- The L2 layer is defined by "persistent REPL". That conflates *retained working state* with
  *Python as the substrate*. Aiden can get L2 with typed, addressable values in a session store
  plus ordinary tools, and skip the kernel. The report's own nanoGPT/PMPP results show the win is
  in **out-of-loop experimentation and accounting**, not in the REPL per se.
- "Score at practical plateau" on multi-day runs is a good metric for them and an unaffordable one
  for a personal project. Aiden's equivalent must be *short tasks with a cost axis* (n=10 tasks ×
  3 seeds × ~$0.05), else we never run evals at all.
- Their 95.5% ARC-AGI-3 number is not a harness ablation, and they say so. Same discipline needed
  for any number we put in Aiden's README.

## Sources

- [1] S. Karten, A. L. Zhang, K. Thomas, S. Müller, E. Bakouch, D. Auras, M. Senghaas, F. Obeid,
  K. Dunas, J. Hagemann, S. Jaghouar, *Prime Agent: A Self-Improving RLM Harness*,
  arXiv:2608.23552v1 — https://arxiv.org/abs/2608.23552 ,
  HTML https://arxiv.org/html/2608.23552v1 ; code
  https://github.com/PrimeIntellect-ai/prime-agent ; docs
  `packages/coding-agent/docs/{architecture,rlm,rlm-runtime,daemon,long-running-agents,skills,tui}.md`
- [2] Prime Intellect, *Recursive Language Models: the paradigm of 2026*,
  https://www.primeintellect.ai/blog/rlm (RLM env-tips ablations)
- [3] *Recursive Language Models*, arXiv:2512.24601, https://arxiv.org/abs/2512.24601
- [4] *Continual Harness: Online Adaptation for Self-Improving Foundation Agents*,
  arXiv:2605.09998, https://arxiv.org/abs/2605.09998
- [5] *PRO-LONG: Programmatic Memory Enables Long-Horizon Reasoning*, arXiv:2607.20064 (source of
  the ARC-AGI-3 autonomous prompt)
- [6] *ARC-AGI-3: A New Challenge for Frontier Agentic Intelligence*, arXiv:2603.24621 ;
  community leaderboard https://arcprize.org/leaderboard/community
- [7] *Measuring Autonomous AI Research* (nanoGPT speedrun), Prime Intellect blog,
  https://www.primeintellect.ai/blog/measuring-autonomous-research
- [8] T. Cunningham (METR), *Metrics of agent ability*,
  https://metr.org/notes/2026-07-24-metrics-of-model-ability/ (fixed-expenditure vs
  practical-plateau metrics)
- [9] *Factorio Learning Environment*, arXiv:2503.09617 ; MazeBench and PMPP-Hard are credited to
  Prime Intellect collaborators (Patience Cave, SinatraS) in [1] §Acknowledgements
- [10] Benchmarks in the long-context suite: Oolong arXiv:2511.02817, LongBench v2 arXiv:2412.15204,
  LongBench Pro arXiv:2601.02872, OBLIQ-Bench arXiv:2605.06235, LongCoT arXiv:2604.14140,
  EmulatorBench (Karten/Zhang/Jaghouar 2026, manuscript),
  *Many-Tier Instruction Hierarchy in LLM Agents* arXiv:2604.09443
- [11] A. Gupta et al., reset-free RL; DAgger; GRPO; *Let's Verify Step by Step* (PRM) — all cited
  as the machinery of the co-learning loop in [4] §3.3
