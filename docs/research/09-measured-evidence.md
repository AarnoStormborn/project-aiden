# Measured Evidence — Numbers to Anchor Aiden's Design Decisions

Every other doc in this folder argues *what* to build. This one records **how much things cost and
how much they help**, so we can argue from numbers instead of taste. Values are copied from the
primary sources listed under each table — several are read straight out of Pi 0.85.1 and Prime
Agent source, several from paper PDFs.

> **Rule for Aiden:** any constant we adopt from here gets written down with its source next to it
> in `aiden/config.py`. Undocumented magic numbers are how a learning harness becomes folklore.

## TL;DR

- Interface design is worth **more than model choice** at the low end: SWE-agent's ACI took GPT-4
  from 11.00% → 18.00% on SWE-bench Lite (**+64% relative**) with the same model. [1]
- But the *shape* of the tool matters more than having more tools: **iterative** search (vim-style,
  next/prev) scored **12.0%** — worse than having **no search tool at all (15.7%)** — while a
  summarized list scored 18.0%. Agents exhaustively page through anything paginated. [1]
- There is a **sweet spot for how much a tool shows**: file viewer at 100 lines = 18.0%,
  30 lines = 14.3%, **whole file = 12.7%**. Both ends of the range hurt. [1]
- More history is not better: full history **15.0%** vs last-5-observations **18.0%**. [1]
- **Costs cluster by outcome.** Resolved SWE-agent trajectories: median **$1.21 / 12 steps**.
  Unresolved: mean **$2.52 / 21 steps**. Raising the budget mostly buys failures — "increasing the
  maximum budget or token limit are unlikely to substantially increase performance". [1]
- **Editing is the unreliable step, not retrieval.** 51.7% of trajectories had ≥1 failed edit; one
  failed edit drops eventual-edit success 90.5% → **57.2%**. Lint-on-edit is worth **+3.0 points**. [1]
- Harness-level context constants have quietly converged: **~16–20k reserved, ~20k kept recent**
  (Pi), **20k buffer / 8k keep / 4k summary** (opencode), **25k tokens** tool-response cap
  (Claude Code), **8192 chars** REPL stdout (Prime RLM), **200k chars** before spill-to-file
  (Goose). [2][3][4][5][6]
- RLM beats compaction by **26%** median, plain CodeAct+sub-calls by **130%**, and Claude Code by
  **13%** — on GPT-5 across their long-context suite — while *increasing* wall-clock time in every
  environment tested. [4][7]
- Online harness refinement is **capability-gated**: Pareto-dominant on a frontier model
  (100% milestones @ $130 vs 98% @ $215), *useless-to-harmful* on a small one (20% → 13–33%). [8]
- Prime Agent's own nanoGPT result is a warning about benchmark selection: **"the choice of harness
  has little effect on final records compared to the noise of the experiment"** — the measurable
  difference showed up in *behaviour* (≈6× more out-of-loop experiments), not score. [5]
- Single-run variance is ~±0.7 points on SWE-bench Lite but **pass@1 17.94 → pass@6 35**. Any
  Aiden eval that runs one seed is measuring noise. [1]

## 1. Tool/interface design has measured effect sizes

SWE-agent, GPT-4 Turbo, SWE-bench Lite, `% Resolved`, one number per config. [1]

| Change vs. SWE-agent ACI | % Resolved | Δ |
|---|---|---|
| **SWE-agent ACI (reference)** | **18.0** | — |
| Editor: remove lint-on-edit | 15.0 | −3.0 |
| Editor: "No edit" (redirect/`sed` only) | 10.3 | **−7.7** |
| Search: summarized match list | 18.0 | — |
| Search: **iterative** (next/prev paging) | 12.0 | **−6.0** |
| Search: none at all | 15.7 | −2.3 |
| File viewer: 30-line window | 14.3 | −3.7 |
| File viewer: **100-line window** | 18.0 | — |
| File viewer: full file | 12.7 | **−5.3** |
| Context: last 5 observations | 18.0 | — |
| Context: full history | 15.0 | −3.0 |
| Context: no demonstration | 16.3 | −1.7 |

Cross-setting comparison of the same model. [1]

| Config (GPT-4 Turbo) | SWE-bench full | cost | SWE-bench Lite | cost |
|---|---|---|---|---|
| RAG (BM25 → one-shot patch) | 1.31% | $0.13 | 2.67% | $0.13 |
| Shell-only interactive agent | — | — | 11.00% | $1.46 |
| Shell-only, no demonstration | — | — | 7.33% | $0.79 |
| **SWE-agent (ACI)** | **12.47%** | **$1.59** | **18.00%** | **$1.67** |
| SWE-agent + Claude 3 Opus | 10.46% | $2.59 | 13.00% | $2.18 |
| RAG + Claude 3 Opus | 3.79% | $0.25 | 4.33% | $0.25 |

Also: agent-vs-RAG is **8–13× the cost for 6.7× the resolution rate**. [1] Budget was capped at
**$4/instance**, over-budget runs auto-submitted their working tree.

Three findings we should treat as laws for Aiden's tooling:

1. **A badly-shaped tool is worse than no tool.** Iterative search *lost* 3.7 points against
   having no search at all, because the agent burned its whole budget paging through matches.
2. **Feedback on the write path is cheap and high-value.** Running a linter as part of `edit`
   (and refusing the edit when it fails) is the single best ratio of effort-to-points here (+3.0).
3. **The transmission belt between turns has an optimum width.** 30 → 100 → whole-file is
   14.3 → 18.0 → 12.7. "Give the model everything" is a measured regression.

### Trajectory economics (GPT-4 Turbo, full set, 286 resolved trajectories) [1]

| Quantity | Resolved | Unresolved |
|---|---|---|
| turns/cost | median **$1.21**, **12** steps | mean **$2.52**, **21** steps |
| submitted before exhausting budget | **93.0%** | 69.0% (overall) |
| trajectory length to finish | mean 14.71 turns, median 12, 75th pct **18** | — |

Action-frequency profile: turns 1–5 are dominated by Reproduction + Localization
(`create`,`edit`,`python` triplet; `search_dir`→`search_file`→`goto` "zoom-in"); from **turn 5
onward** the two most frequent actions at every turn are `edit` and `python`. Submissions are
normally distributed from turn ~10. **Aiden's UI should visualise "am I in the edit↔run loop or
still searching?"** — it is a real progress signal, and running out of turns *while still
searching* is the dominant failure shape.

### Failure taxonomy, LM-classified (n=248 unresolved, Lite; 87% agreement with human labels) [1]

| Failure mode | Share |
|---|---|
| Incorrect Implementation | 39.9% |
| **Failed to Recover from Edit** (cascading edit loop) | **23.4%** |
| Failed to Find Edit Location | 12.9% |
| Overly Specific Implementation | 12.1% |
| Gave Up Prematurely | 4.8% |
| Failed to Find Relevant File | 2.4% |
| Can't Reproduce | 2.4% |
| Ran Out of Time | 2.0% |

Edit reliability detail: **1,185 / 2,294 (51.7%)** trajectories hit ≥1 failed edit. Any given edit
attempt eventually succeeds **90.5%** of the time — but **57.2%** after one prior failure. That
compounding is the 23.4% bar above. Implication for Aiden: after the *first* failed structural edit
on a file, change strategy (re-read the region, shrink the edit, or fall back to `write`) instead
of retrying harder. This is also why Pi's `edit` tool requires unique `oldText` and reports
per-edit failure separately.

### Variance / pass@k (Lite, 6 runs of the same config) [1]

| Runs | 1 | 2 | 3 | 4 | 5 | 6 | mean |
|---|---|---|---|---|---|---|---|
| % Resolved | 17.33 | 18.00 | 18.00 | 18.67 | 17.33 | 18.33 | **17.94 ± 0.49** |

| Pass@k | 1 | 2 | 3 | 4 | 5 | 6 |
|---|---|---|---|---|---|---|
| % | 17.94 | 23.89 | 27.35 | 29.67 | 31.33 | **35.00** |

Score doubles from k=1 to k=6. For Aiden: report **mean ± spread over ≥3 seeds**, and a
`pass@k`/`best@k` column whenever we compare harnesses; a 1-point difference is inside noise.

## 2. Context and output budgets actually shipped in production harnesses

| Harness | Constant | Value | Notes |
|---|---|---|---|
| Pi | `DEFAULT_MAX_LINES` | **2000** | per `read` call [3] |
| Pi | `DEFAULT_MAX_BYTES` | **50 KB** | per tool call, whichever hits first [3] |
| Pi | `GREP_MAX_LINE_LENGTH` | **500** chars | per matched line [3] |
| Pi | `reserveTokens` | **16 384** | headroom kept for the response [2] |
| Pi | `keepRecentTokens` | **20 000** | tail preserved by compaction [2] |
| Pi | token estimator | **4 chars/token**, **4 800 chars/image** | cheap heuristic, not a tokenizer [2] |
| Pi | tool result in summary input | 2 000 chars | truncation before summarization [2] |
| opencode | `TOOL_OUTPUT_MAX_CHARS` | **2 000** | when feeding compaction [6] |
| opencode | compaction | buffer **20 000**, keep **8 000**, `SUMMARY_OUTPUT_TOKENS` **4 096** | [6] |
| Goose | `DEFAULT_LARGE_TEXT_THRESHOLD` | **200 000 chars** | over → write to owner-only temp file, hand back a path [9] |
| Claude Code | tool response cap | **25 000 tokens** (default) | [10] |
| Prime RLM (verifiers) | REPL stdout per turn | **8 192 chars** | the forcing function for programmatic processing [7] |
| Prime RLM | per-REPL-call timeout | **120 s** | model is told the timeout and elapsed time [7] |
| Prime Agent | refine output cap | **32 000** tokens (writer), **4 096** (gate), +1 024 overhead | reasoning off; caps derived from window [11] |
| OpenHands | `LLMSummarizingCondenser` | `max_size=240` events, `keep_first=2` | counts *events*, not tokens [12] |
| Aider | `--map-tokens` | **1024** default | auto-expanded when no files in chat [13] |
| Aider | `ChatSummary` reserve | **512 tokens** under `max_input_tokens` | recursive folding [13] |
| SWE-agent | file viewer window | **100 lines** | optimum from the table above [1] |
| Continual Harness | refine every `F` steps after warm-up `W` | paper values, gameplay scale | [8] |

Convergence worth noting: **keep ~20k of recent context, reserve ~16k for output, and clip any
single tool result to 2–50 KB.** Pi/Prime's numbers are per-call; Goose's is a spill threshold;
Claude Code's is per-response. Aiden should have all three knobs, and per-turn *aggregate* budget
on top (no harness surveyed caps the total output of a 5-tool batch — an easy win to prototype).

## 3. Retrieval strategy: measured trade-offs

| Method | Evidence | Source |
|---|---|---|
| `cat`/`cd`/`ls` chains | "extremely inefficient" exploration patterns; grep/find "occasionally produce many lines of irrelevant results" | [1] |
| Summarized grep (file names only, no context lines) | **18.0%** (best); extra context per match "proved to be confusing for the model" | [1] |
| BM25 file retrieval as a *one-shot* | 2.67% on Lite — retrieval alone solves almost nothing, it's only a starting point | [1] |
| Tree-sitter symbol tags + **PageRank over a file/dependency graph**, ranked into a token budget (Aider repo map) | shipped default 1024 tokens, expanded when nothing is in chat; the map "is the context strategy" | [13] |
| Semantic/embedding retrieval | no first-party measurement found in these sources; r-02 covers it | — |
| Pi's actual default | `grep` shells out to ripgrep (auto-downloaded), respects `.gitignore`, match-capped, line-clipped; no embeddings at all | [3] |

Aiden's v1 search should be **ripgrep + a symbol map**, matching what the two named inspirations
ship, and the repo map is the one idea from Aider worth rebuilding (it is cheap, deterministic,
and it puts *structure* rather than prose in the prompt).

## 4. Scaffolding effects on long-context quality (RLM)

Medians across the paper's evaluated long-context benchmarks, GPT-5. [4][7]

| Comparison | Median lift |
|---|---|
| RLM vs **compaction** | **+26%** |
| RLM vs **CodeAct with sub-calls** | **+130%** |
| RLM vs **Claude Code** | **+13%** |
| Inputs handled | up to **2 orders of magnitude** beyond the context window |
| RLM-Qwen3-8B vs Qwen3-8B (post-trained on the scaffold) | **+28.3%** avg; approaches vanilla GPT-5 on 3 tasks |

Per-environment behaviour (Prime's ablations, GPT-5-mini, 50 prompts, seed-fixed) — the honest
part [7]:

| Environment | RLM − LLM | Token/time effect |
|---|---|---|
| DeepDive (deep research, tool-heavy) | **worse** without `<env_tips>`; beats baseline only once told to decompose + `llm_batch()` | big main-context compression; most tokens moved to sub-LLMs |
| Oolong *real* subset | **clear win** (esp. ~1.5M chars ≈ 300–400k tokens) | more total tokens; tips shift prompt→sub-LLM |
| Oolong *synth* | **worse** than LLM | — |
| Oolong *synth-with-labels* | RLM used **0** sub-LLM calls, solved by regex, perfect at all lengths | — |
| math-python | **worse** (suspected benchmark overfit to a plain Python tool) | main-model tokens up, quality down |
| verbatim-copy | better, different strategy | more tool calls/turns |
| timing | — | **worse in every environment** |

Read: RLM's win is on tasks where **the input does not fit**; on inputs that do fit, the scaffold
is a tax. So Aiden's kernel/RLM layer should be **opt-in per task type**, not the default loop.

## 5. Continual learning: measured effect sizes

Continual Harness, Pokémon Red/Emerald, milestone-vs-button-press cost, ≥3 seeds, medians. [8]

| Condition | Emerald result | Effect of refinement |
|---|---|---|
| Gemini-3-**Pro**, `H_min` | 98% milestones @ **$215** | — |
| Gemini-3-Pro, `H_CH` from scratch | **100% @ $130** | **~40% cheaper**, strictly Pareto-dominant |
| Gemini-3-Pro, bootstrap-updating | 96–100% @ $110–140 | inheritance + continued refinement |
| Gemini-3-**Flash**, `H_min` | 77% @ $30 | — |
| Gemini-3-Flash, `H_CH` | 80% @ $42 (bootstrap-updating); other variants high variance | **marginal** |
| Gemini-3-**Flash-Lite**, `H_min` | 20% @ $11 | — |
| Flash-Lite, any `H_CH` | **13–33%** at ≥ cost | **worse than doing nothing** |

| Skill self-improvement metric | Value |
|---|---|
| navigation-skill path-cost deficit vs **Dijkstra oracle**, from-scratch run | ~50% → **single digits** within one 24h episode |
| navigation-skill invocations, `H_min` vs `H_CH` | **0** vs **hundreds** per 24h |
| where refinement concentrates | a small subset of navigation + battle components; updates **persist rather than converge** |
| bootstrap-updating vs bootstrap-frozen (Red) | updating more efficient at **every** milestone → refinement signal compounds across episodes |

Co-learning stage (open-source Gemma-4, reset-free DAgger+PRM, K=256 steps/iteration, batch 1):
sustained milestone advance from both beginning-of-game and mid-game checkpoints; untrained
baseline advances 0; neither SFT warm-up nor offline GRPO alone produced "meaningful milestone
advancement on its own". [8]

Aiden's takeaway is in the **capability floor row**: gate learning features on model strength, and
report cost-vs-progress curves per model, not one aggregate number.

## 6. Harness-vs-model attribution: what Prime measured

| Task | Result | What it tells us |
|---|---|---|
| ARC-AGI-3 (RHAE Best@1) | **30% → 95.5%** vs native/other harnesses; but their own Claude Code/Codex reruns scored **below** vendor-reported numbers, so they deferred to official figures [5] | public numbers are not comparable unless you rerun the harness yourself |
| nanoGPT speedrun (3 models × 2 harnesses, 2–3 seeds) | "harness has **little effect on final records compared to the noise**"; 85.5 h autonomous run, 19 validated records [5] | pass/fail can be blind to real harness effects |
| nanoGPT *behavioural* metric | DeepSeek V4 Pro **≈6× more** out-of-loop experiments per training run under Prime Agent than Claude Code; Kimi K3 built a probe fn → ~90 screening experiments covering **all 19** validated records, while its own CLI did file edits only [5] | **behaviour metrics found the effect that score metrics missed** |
| PMPP-Hard (GPU kernels, fixed within-model wall-clock budgets) | solve rates close, ordering **reverses** between model groups; but "substantial improvement in token usage" → "token-for-token, Prime Agent has an advantage" [5] | fixing *time* hides *cost*; report both axes |
| EmulatorBench (16 reconstructions) | competitive/matching; "For Opus, our runs surprisingly failed … despite successful tool-call responses" [5] | a tool call succeeding says nothing about the outcome |
| Factorio (7-day Sonnet 5) | 23.4M output tokens, 24/196 technologies, 71% advanced-circuit, **633 depth-1 subagents over 149 dispatch waves, ≤7 concurrent**; a destructive world reset reverted 5→1 technologies [5] | delegation is wide-and-shallow in practice; irreversible state is the hazard |

Metric framework they cite (METR, *Metrics of agent ability*): **score at fixed expenditure**
(tokens / cost / time) and **score at practical plateau** — i.e. the *shape* of performance over
expenditure. [5][14]

## 6b. Infrastructure noise: the confounder that swallows most harness comparisons

Anthropic, *Quantifying infrastructure noise in agentic coding evals* (Gian Segato et al.).
Verified first-hand from the article. [16]

| Measurement | Value |
|---|---|
| Terminal-Bench 2.0, most- vs least-resourced config (same model, same harness, same tasks) | **6 pp**, p < 0.01 |
| Infra error rate: strict 1× per-task spec | **5.8%** of tasks |
| …at 3× headroom | **2.1%** (p < 0.001) — score change **within noise, p = 0.40** |
| …uncapped | **0.5%**; total lift over 1× = **+6 pp** success |
| 3× → uncapped separately | infra errors −1.6 pp, **success +~4 pp** (resources start solving tasks, not just stabilising them) |
| SWE-bench crossover (227 problems × 10 samples, RAM 1×→5×) | **+1.54 pp** — same direction, smaller effect |
| Naive binomial CIs at these sample sizes | **1–2 pp** |
| Observed spread across the *moderate* resource range on TB 2.0 | just under **2 pp** |

Their recommendation, verbatim in spirit: **specify both a guaranteed allocation and a hard kill
limit per task** (not one pinned value), calibrate the band so floor-vs-ceiling scores differ by
less than noise, and report the multiplier. And the number Aiden must memorise:

> "leaderboard differences **below 3 percentage points** deserve skepticism until the eval
> configuration is documented and matched." [16]

Also observed but *not* quantified: pass rates fluctuate with **time of day**, likely from API
latency variation. Their advice for public benchmarks: run at multiple times and on multiple days.

**Why the mechanism bites.** Container runtimes separate *reservation* from *kill threshold*; when
they're equal there is zero headroom, so a transient spike OOM-kills a container that would
otherwise have succeeded. Below ~3× you are fixing reliability; above it you are *changing what the
benchmark measures* — tight limits reward lean standard-library strategies, generous limits reward
agents that brute-force `pandas`/`scikit-learn` installs and heavy subprocesses. Both are legitimate
tests; one score cannot represent both.

### What this forces in Aiden

1. Every eval run writes an **environment manifest** next to the result: cpu/mem guarantee + hard
   limit, disk, timeout, sandbox provider, model id + reasoning effort, harness git SHA,
   **harness-state (continual-learning) hash**, prompt/skill versions, wall-clock start.
2. **No claim is published from a single seed or a single time of day.** ≥3 seeds, spread reported.
3. Any delta **< 3 pp is "no change"** in our own tables unless a paired test says otherwise.
4. Report **two resource columns** (guarantee, ceiling) in every result table — not one "config".
5. Prefer *behavioural* deltas over score deltas when the task set is small (§6 nanoGPT row), since
   noise dominates the score at personal-project scale.

## 7. Instrumentation targets for Aiden (derived, then verified)

Every number above is only obtainable if the harness records it. Minimum instrumentation, from the
sources that actually report measurements:

| Must record | Where that comes from |
|---|---|
| tokens in/out/**cacheRead**/**cacheWrite** per assistant message, per model | Pi `usage` on entries incl. `CompactionEntry` [2] |
| cost per transcript entry **including** summarization and child usage | Pi `Tools/summaries` bucket; Prime `child_usage_attributed` [2][11] |
| tool call: name, args hash, duration, exit status, output bytes, **bytes truncated**, whether the model re-paged | Anthropic: "redundant tool calls → rightsizing pagination"; Claude Code metrics guidance [10] |
| failed-edit chains (per file) | the 90.5%→57.2% cliff [1] |
| turn index of first `edit`/`test` cycle and of final submit | trajectory phase analysis [1] |
| per-tool invocation histogram over turns | Figure 7 / density plots [1] |
| harness-state version at every eval run | Aiden addition: no source harness does this |
| outcome-classified failure label (LLM-judged, with measured human agreement) | 87% agreement method [1] |

## Sources

- [1] C. E. Jimenez et al., **SWE-agent: Agent-Computer Interfaces Enables Automated Software
  Engineering**, arXiv:2405.15793 (NeurIPS 2024) — PDF read locally; Table 1 (main results),
  Table 3 (ACI ablations), Table 10 (pass@k), Table 13 (episode outcomes), Figure 7/8
  (action frequency, failure modes), §B.3.1–B.3.3 (turns, trajectory phases), §B.4 (failure
  taxonomy), §4 (baselines, metrics, $4 budget). https://arxiv.org/abs/2405.15793 ;
  docs https://swe-agent.com/0.7/background/aci/
- [2] Pi `docs/compaction.md` (`reserveTokens` 16384, `keepRecentTokens` 20000, cut-point rules,
  estimator 4 chars/token) + `docs/session-format.md` (usage on entries)
- [3] Pi `dist/core/tools/truncate.js`, `read.js`, `grep.js`, `edit.js` (constants quoted verbatim)
- [4] A. Zhang et al., **Recursive Language Models**, arXiv:2512.24601v3 — abstract numbers
  (26% / 130% / 13%, 2 orders of magnitude, RLM-Qwen3-8B +28.3%). https://arxiv.org/abs/2512.24601
- [5] S. Karten et al., **Prime Agent: A Self-Improving RLM Harness**, arXiv:2608.23552v1 §3
  (ARC-AGI-3, nanoGPT, EmulatorBench, PMPP-Hard, Factorio, MazeBench)
- [6] opencode `SessionCompaction` constants (buffer/keep/`SUMMARY_OUTPUT_TOKENS`),
  `TOOL_OUTPUT_MAX_CHARS` — see [01-harness-anatomy.md](01-harness-anatomy.md) §context
- [7] Prime Intellect, **Recursive Language Models: the paradigm of 2026**,
  https://www.primeintellect.ai/blog/rlm (8192-char REPL cap, 120 s timeout, `answer` dict,
  `llm_batch`, `<env_tips>`, per-environment token/time results)
- [8] S. Karten et al., **Continual Harness: Online Adaptation for Self-Improving Foundation
  Agents**, arXiv:2605.09998v1 §4.3–4.6, Fig 5–8 (button-press cost, Pareto plane, Dijkstra-oracle
  path deficit, CRUD concentration). https://arxiv.org/abs/2605.09998
- [9] Goose `DEFAULT_LARGE_TEXT_THRESHOLD` / large-text temp-file redirect
- [10] Anthropic Engineering, **Writing effective tools for agents — with agents**,
  https://www.anthropic.com/engineering/writing-tools-for-agents (25k token cap, concise vs
  detailed responses, semantic IDs over UUIDs, evaluation + metrics guidance)
- [11] Prime Agent `packages/coding-agent/src/core/refinement/refinement.ts`
  (`REFINEMENT_MAX_OUTPUT_TOKENS = 32_000`, `AUTO_REFINE_REVIEW_MAX_OUTPUT_TOKENS = 4_096`,
  `REFINEMENT_CONTEXT_OVERHEAD_TOKENS = 1_024`)
- [12] OpenHands SDK `LLMSummarizingCondenser(max_size=240, keep_first=2)`
- [13] Aider **Repository map**, https://aider.chat/docs/repomap.html (`--map-tokens` 1k default,
  graph ranking, dynamic expansion) + Aider blog https://aider.chat/2023/10/22/repomap.html
- [14] T. Cunningham (METR), **Metrics of agent ability**,
  https://metr.org/notes/2026-07-24-metrics-of-model-ability/
- [16] G. Segato et al., **Quantifying infrastructure noise in agentic coding evals**, Anthropic
  Engineering, https://www.anthropic.com/engineering/infrastructure-noise (all figures in §6b read
  from the article; TB 2.0 runs on GKE, six resource configurations, same model/harness/tasks)
- [17] H. Wang / LangChain, **Improving Deep Agents with harness engineering**,
  https://www.langchain.com/blog/improving-deep-agents-with-harness-engineering — `deepagents-cli`
  **52.8% → 66.5%** on Terminal-Bench 2.0 (89 tasks) with the model fixed at `gpt-5.2-codex`, Harbor
  orchestration + Daytona sandboxes, LangSmith traces; only prompt, tools and middleware changed.
  Per-knob findings: `xhigh`-only reasoning scored **53.9%** (timeouts) vs **63.6%** at `high`,
  final **66.5%** with a "reasoning sandwich" (xhigh plan → high build → xhigh verify);
  `LoopDetectionMiddleware` counts per-file edits and suggests reconsidering after N;
  `PreCompletionChecklistMiddleware` forces a verification pass before exit; `LocalContextMiddleware`
  maps `cwd` + available interpreters at start; Claude Opus 4.6 hit **59.6%** on an earlier harness
  that had not been iterated for Claude — i.e. harness tuning is model-specific. Trace dataset is
  public.
- [15] Related docs in this folder: [00-reference-source-study.md](00-reference-source-study.md),
  [01-harness-anatomy.md](01-harness-anatomy.md),
  [07-continual-learning-papers.md](07-continual-learning-papers.md),
  [08-prime-agent-tech-report.md](08-prime-agent-tech-report.md)
