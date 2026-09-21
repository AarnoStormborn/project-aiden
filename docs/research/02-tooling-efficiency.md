# Tooling Efficiency: Designing Aiden's Agent–Computer Interface

Research for Project Aiden — how small the tool surface can be, and how to stop tool output
from eating the context window. Includes first-hand measurements taken on this machine.

## TL;DR

- **Seven tools are enough**: `read`, `grep`, `glob`, `edit`, `write`, `bash`, `web_fetch`.
  Everything else is a plugin. mini-SWE-agent reaches >74 % on SWE-bench Verified with a
  *single* tool (`bash`) at $0.07–$0.75 per instance [29][30]; Anthropic hit 49.0 % pass@1 with
  two tools (`bash` + a stateful file editor) [6]. Tool count is not the lever; output shaping is.
- **A tool earns its keep by doing one of three things**: granting a capability, *compressing*
  an observation, or adding a validation gate. Only the third and second are harness work.
  The SWE-agent lint gate alone is worth 3.0 pp; removing the edit tool costs 7.7 pp [1].
- **Output budgets are the highest-leverage knob we own.** Measured here: a context-free
  `rg -n -w logger --type py` over a 104 MB corpus returns ≈90 k tokens in one call; the same
  query with `-l` costs ≈650 tokens. One unbounded grep can consume half a 200 k window.
- **Never give the model raw search context by default.** `-C 3` costs 6.7× the tokens of bare
  `-n` for the same 31 hits; `--json` costs 4.9×. Emit compact text to the model, keep structure
  internally (dual representation), and let `context=` be an explicit, model-visible argument.
- **Patch format: exact-string replace (`old`/`new`, unique match required) as the primary**, with
  whole-file `write` as the fallback for new/small files. Generated unified diffs applied on only
  48.7 % of GPT-4 "oracle-collapsed" attempts (1,116/2,292), and 684 of those (61 %) only via the
  automatic repair pass [28]; on aider's edit leaderboard strong models emit SEARCH/REPLACE with
  99.2 % format compliance for 84.2 % correct completions (o1, claude-3-5-sonnet), while
  gemini-exp-1206 scores 80.5 % on `whole` vs 69.2 % on `diff` — format fit is model-specific
  [21][22]. Adaptive selection is worth more than syntax choice:
  SWE-Edit gains +2.1 pp resolved and −17.9 % inference cost by splitting viewing from editing [32].
- **Search: ripgrep-first, no embeddings in the core.** `rg` was 6.3× faster than `grep -rn`
  here (0.175 s vs 1.094 s over 7,386 files) and 10.69× faster than GNU grep on the kernel tree
  upstream [40]. LSP retrieval *costs* +6 %→+118 % tokens on symbol localization and fails 3/4
  multi-file renames [35]. Dense/BM25 retrieval does not reliably beat agents that can grep [38].
- **Read-state caching is cheap and prevents a class of silent corruption.** Content hashing
  costs 2.2 GB/s measured — hash on read, don't trust mtime alone. Read-before-edit plus
  stale-view rejection is the standard guard in shipped harnesses [14][27].
- **Parallelise only read-only tools.** Mutating and stateful-shell calls stay serial. The saving
  is in model round-trips (seconds), not tool execution (1.7–185 ms measured).
- **Sandbox by OS primitive + proxy, not by prompt.** Codex: bubblewrap+seccomp+netns with a
  TCP→unix→TCP proxy bridge [20]; Claude Code: Seatbelt/bwrap + domain allowlist, and it still
  shipped a config-injection sandbox escape fixed in v2.1.2 [16][17]. pi deliberately ships *no*
  sandbox and outsources isolation to a container/micro-VM [55] — the honest default for a local
  harness. Aiden follows pi, since we orchestrate it.
- **Instrument 8 metrics from day one**, on the existing session log: tokens/task, tool-calls/turn,
  output-bytes-per-call, wasted-output ratio, edit first-pass rate, re-read ratio, truncation rate,
  cache-hit ratio. Everything else is decoration (see §Instrumentation plan).

## 1. What a tool is actually for

Anthropic's framing is that capable agents come from *simple, composable* patterns, not from a
wide tool catalogue [4]. Concretely, a tool earns its context share by doing one of three jobs, in
descending value:

1. **Grant a capability** the model does not have (write a file, hit the network, run a test).
   This is table stakes and mostly free: `bash` already grants capability #1 for everything
   except *structured* file mutation.
2. **Compress the observation.** A tool exists to return *less text* than `cat`/`grep` would.
   SWE-agent's file viewer returning 100 lines instead of the whole file is worth 5.3 pp
   (100 lines: 18.0 % vs full file: 12.7 % on SWE-bench Lite) [1]. Claude Code's `WebFetch`
   runs a small fast model over the page and hands Claude the answer, not the HTML — "lossy by
   design" and explicitly better for the context budget [14].
3. **Gate with validation.** Lint before accepting an edit; require a unique match; reject a stale
   read; run the reproducer. SWE-agent: `edit` with linting 18.0 %, without 15.0 % [1]. Anthropic
   makes the same point about tools being "error-proof, not merely powerful" [6].

The cost side is a simple sum. For a session with `T` turns and a tool set `K`:

```
cost ≈ Σ_t [ prompt_t (cached? 0.1× : 1×) + out_t ]  +  |K| × schema_tokens  +  Σ results_t
```

`schema_tokens` is amortised over every turn of the session, which is why it is a harness bug
when it is large: Anthropic documents ~55 k tokens of tool definitions in a typical multi-server
MCP setup before any work happens, and its Tool Search Tool cuts that by 85 %+ by loading only
the 3–5 tools a request needs [10]; its "code execution with MCP" post reports 150 k → 2 k
tokens on a Drive→Salesforce workflow by making the model *write code* that calls tools instead
of binding every schema into context [5]. Both are arguments for a tiny always-on core plus
deferred capabilities.

## 2. Tool surface for Aiden

Core seven, always in context. Design budgets are proposals, calibrated against shipped harnesses
(pi: 50 KB ≈ 10 k tokens / 2,000 lines per tool result [55]; Claude Code: 30,000 chars per Bash
result, middle-truncated, `head_limit` 250 for Grep [14][15]; SWE-agent: 100-line viewer [1][2];
Gemini CLI: 40,000 chars per tool output [44]). The frequently-quoted 25 k-token `Read` cap and the
exact Grep caps come from bundled-source/community dumps rather than official docs, so we treat
them as order-of-magnitude only [56].

| Tool | Args | Output budget | Failure mode |
|---|---|---|---|
| `read` | `path` (abs), `offset?`, `limit?`, `symbol?` | ≤400 lines **and** ≤16 KB (~4 k tok). On whole-file overflow return page 1 + `PARTIAL view: got 1-400 of 15,716 lines; pass offset/limit` [14] | not found; dir → suggest `glob`; binary → refuse, offer `grep -a`; offset past EOF → return total lines, never "empty" (model must not conclude "no content") |
| `grep` | `pattern`, `path?`, `glob?`, `type?`, `mode∈{files,content,count}`, `context?=0`, `head?=200`, `fixed?` | ≤200 matching lines or 16 KB, head-truncated; always append `… +N files / M total matches (0.18s, rg)`. `mode=count` for sizing before `content` | no matches → `No matches in 7,386 files (0.17s)` — an explicit non-empty string, per SWE-agent's "ran successfully and did not produce output" trick [2]; regex syntax error → echo the offending pattern + a fixed-string hint |
| `glob` | `pattern`, `path?`, `sort∈{mtime,name}` | ≤500 paths, mtime-desc; drop dirs | no matches → explicit; over cap → `+N more, narrow the pattern` |
| `edit` | `path`, `old`, `new`, `all?`, `dry_run?` | The applied unified diff (≤60 lines) + post-edit diagnostics. Never echo the file | 0 matches; >1 matches (ambiguous) → report count + the N candidate line numbers; stale view (hash mismatch) → refuse, show current window; lint/parse failure → revert and emit the 3-part message (error, would-have-been, original) [1] |
| `write` | `path`, `content`, `overwrite?` | `created <path> (n lines, b bytes)` + diff-stat vs prior content | dir missing; non-empty without `overwrite`; stale view; >64 KB → suggest splitting (output tokens come back later as input) |
| `bash` | `command`, `cwd?`, `timeout_ms?=30000`, `background?`, `yield_ms?` | **tail**-truncate 400 lines/30 KB, spill full output to a file and return its path (pi does exactly this with `fullOutputPath`) [55]; `exit=N`; empty → explicit success line | timeout → return partial stdout + "still running, poll <id>"; permission denied (sandbox); non-zero exit is *data*, not an error |
| `web_fetch` | `url`, `question?`, `raw?`, `max_tokens?` | ≤6 k tokens. With `question`: extractive answer + `[n]` provenance (Claude Code model: small fast extractor [14]). `raw`: head-truncate | non-2xx; redirect → return target and let a second call follow [14]; >deadline (60 s) → fail with partial; robots/auth wall → say so, don't return a login page as "content" |

Also-planned, **deferred** (schema not in context until discovered): `symbols`/`rename` (LSP or
tree-sitter plugin), `task` (sub-agent with its own window), `note` (append-only memory file),
`skill`, `mcpScript`. Anthropic's threshold for deferring is ≥10 tools or ≥10 k tokens of schemas
[10]; Aiden's core is 7 tools ≈ 1.5–2.5 k tokens of description budget (see table below).

### Why these seven and not more

- `read`/`bash cat` differ by *windowing + line numbers + caching state*. Line numbers are not
  cosmetic: SWE-agent's `edit` is grounded in viewer line numbers specifically to remove model
  arithmetic, which was a measured failure source [1].
- `grep` exists as a *tool* (not `bash rg`) so the harness can force `head`, `mode`, and timing
  metadata, and so it can be parallelised and permission-checked independently [3][11].
- `glob` vs `grep`: listing is a different query shape, cheap, and it is what makes just-in-time
  retrieval work — keep paths in context, not contents [7].
- `edit` and `write` are separate because their risk and their budgets differ; merging them into
  one "apply patch" tool is what Codex does with a *freeform* (non-JSON) grammar instead [18][19].
- `bash` is the escape hatch that keeps the surface small. It is also the reason every other tool
  must be explicit about being *preferred* for reads — otherwise models `cat` everything.
- `web_fetch` is the research half of the harness and the only tool where we should let the
  harness spend a *second model* to compress output [14].

Schema cost, per tool (measured as `len(json.dumps(tool)) / 3.6`; 3.6 chars/token is the code-heavy
ratio used throughout this doc — pi's docs use the more generous 5.0 for mixed tool output [55]):

| Surface | Schema cost | Source |
|---|---|---|
| Aiden core 7 (prose descriptions ≈ this table, 1 short example each) | ≈1.5–2.5 k tokens | design target |
| Typical multi-server MCP catalogue | ≈55 k tokens | [10] |
| MCP catalogue + Tool Search Tool | ≈8.5 k (−85 %) | [10] |
| Schemas moved into executed code | ≈2 k | [5] |

## 3. Token economics with numbers

### 3.1 First-hand measurements (2026-09-15)

Environment: Apple M1 Max, macOS 14.8.7, warm page cache, ripgrep 15.1.0, Python 3.11.6.
Corpus: the local Python 3.11 install — **7,386 `.py` files, 103.9 MB, 20,097 total files**.
Reproduce with: `find <corpus> -name '*.py' -exec cat {} + | wc -c`.

| Measurement | Value | Read as |
|---|---|---|
| `rg -n -F "def load_module"` (31 hits) | 0.175 s, 4,423 B ≈ **1.2 k tok** | baseline |
| same `--json` | 0.198 s, 21,544 B | **4.9× bytes** for structure |
| same `-n -C 3` | 29,669 B | **6.7× tokens** for context lines |
| same `-l` (files only) | 2,357 B | 0.53× — the right default for "where is it" |
| `grep -rn --include='*.py'` | 1.094 s | **6.3× slower** than `rg` |
| `rg --files` (20,097 files) | 0.029 s | listing is free; reading is not |
| `rg -n -w logger --type py` | 2,024 lines, 323 KB ≈ **90 k tok** | why `head` is mandatory |
| numbered whole read, `pydoc_data/topics.py` (15,716 ln) | ≈**233 k tok** | one read blows the window |
| 100-line numbered window, same file | ≈1.4 k tok | **171× ratio** |
| symbol outline, `typing.py` (3,487 ln) | 1,333 tok vs 33,081 full | outline = **4.0 %** |
| Python-`ast` symbol index, whole corpus | 165,818 syms in 16.6 s (437 files/s) | one-time cost, ~0.16 tok/file of context |
| median file parse / 756 KB file | 0.9 ms / 6.2 ms | reparse on edit is free |
| name lookup in sorted index | 0.25 µs | indexing cost is build, not query |
| `sha256` of 2,000 files (21.9 MB) | 0.010 s → **2.2 GB/s** | hash everything; mtime is not a substitute |
| `stat()` ×2,000 | 5.2 ms (2.6 µs/file) | staleness check ≈ free |
| process spawn `/usr/bin/true` / `rg --version` | 1.70 ms / 2.79 ms | tool-call floor; timeouts must be ≫ this |

Two of these are design decisions in disguise: the **171× read ratio** (why paging beats whole-file
reads even when the model "wants to see the file") and the **6.7× context-line multiplier** (why
`grep` must default to `context=0` and let the model ask for more, one narrow call at a time).

### 3.2 Task-level evidence

| System | Setting | Result | Tokens / cost |
|---|---|---|---|
| SWE-agent (Table 1) [1] | SWE-bench Lite, RAG scaffold | 2.67 % | $0.13 |
| SWE-agent (Table 1) [1] | shell-only, GPT-4 Turbo | 11.00 % | $1.46 |
| SWE-agent (Table 1) [1] | tuned ACI, same model | **18.00 %** | $1.67 |
| SWE-agent (Table 3) [1] | viewer window 30 / **100** / full | 14.3 / **18.0** / 12.7 % | mid-size wins, non-monotonic |
| SWE-agent (Table 3) [1] | search summarized / iterative / none | 18.0 / 12.0 / 15.7 % | *iterative* paging is worse than no tool |
| SWE-agent (Table 3) [1] | history last-5 obs / full | 18.0 / 15.0 % | collapse old observations |
| SWE-agent (Table 3) [1] | edit w/ lint gate / w/o / absent | 18.0 / 15.0 / 10.3 % | validation is worth 7.7 pp |
| SWE-agent (App. B) [1] | episode budget | 3 malformed actions ⇒ terminate | $4 cap ≈ 30–40 turns |
| SWE-agent (App. B) [1] | failure taxonomy | 23.4 % "Failed Edit Recovery" | edit *reliability*, not reasoning, is the ceiling |
| Anthropic SWE-bench harness [6] | 2 tools, 200 k token cap | 49.0 % pass@1 | "hundreds of turns" on hard tasks |
| Agentless [31] | no agent loop; localize→edit→validate | 32.00 % (Lite) | **$0.70** |
| mini-SWE-agent (bash-only board) [29][30] | 1 tool, ~100-line agent | 70.6–76.8 % | **$0.07–$0.75** /instance |
| Anthropic multi-agent research [52] | 1 Opus lead + Sonnet workers | +90.2 % internal eval | **≈15×** tokens of a chat (agents alone ≈4×) |
| Anthropic context editing [8] | 100-turn search eval | +29 % (editing) / +39 % (+memory tool) | **−84 %** tokens |
| Epoch AI [51] | SWE-bench Verified, 500 tasks | — | 100–150 M tokens ⇒ **200–300 k tok/task**, mostly cached |

The shape of the curve is consistent: the *interface* moves results by double digits (18.00 vs
11.00 % with the same model [1]) while cost per resolved task spans an order of magnitude
($0.07 → $0.75 [29]). Both are harness choices.

### 3.3 Where the money actually goes: caching

Cache reads cost **0.1×** base input, 5-minute cache writes **1.25×**, 1-hour writes **2×** [13].
For a monotone-appending transcript, that means: a 40-turn run whose prompt grows linearly to
30 k tokens has re-sent ≈0.6 M prompt tokens; at $3 / MTok base that is $1.80 uncached and
≈$0.18 if the prefix hits — provided nothing invalidates the prefix. Consequences for tool design:

- **Append-only history.** Never rewrite old tool results in place; that re-bills the whole suffix.
  Context editing "prunes stale tool results" [8], so it trades a 100 %-price rewrite for an 84 %
  token cut — worth it only near the window limit (server-side compaction in the API triggers by
  default at 150 k input tokens, minimum 50 k [9]). pi does the same thing at serialization time
  for compaction only (tool results trimmed to 2,000 chars when building the summary) [55].
- **Put stable bytes first**: system prompt → tool schemas → repo map. aider's `--cache-prompts`
  caches exactly that prefix: system prompt, read-only files, repo map, editable files [57].
- **Truncate with a head cut, not a summarize-in-place.** Anthropic's own guidance is to return
  high-signal fields with pagination and *useful truncation defaults* [3]; pi exports
  `truncateHead` (reads, search results) vs `truncateTail` (logs, command output) for the same
  reason and mandates telling the model where the full output went [55]. Gemini CLI settled on a
  20 % head / 80 % tail split because errors land at the end of shell output [44].

```
              bytes requested            bytes admitted to context
read(path)  ┌───────────────┐  head/tail ┌────────────┐  budget   ┌───────────┐
 raw file   │ 756 KB        │  filter    │ ≤16 KB     │ gate      │ ≤4 k tok  │──▶ model
            │ 233 k tok     │ (drop     │ 1.4-4 k    │ (spill    │ + PARTIAL │
            └───────────────│  dupes,   │ ───────────│  rest to  │  notice   │
                            │  blobs)   │  +path ptr)│  a file)  └───────────┘
                            └───────────┘            ▼
                                        full output file (re-readable by path, never re-sent)
```

### 3.4 Structured results: pay the byte tax only when it buys something

`--json` quadrupled bytes here for zero decision value. But structure is not for the model — it is
for the harness, the TUI, and the metrics store. Both shipped harnesses in our stack already split
these channels: pi returns text to the model while persisting structured blocks plus `details`, and
its RPC layer carries `truncated` + `fullOutputPath` alongside the trimmed `output` [55]. Aiden
copies that: **canonical structured record internally, rendered compact text to the model**, with
the same byte budget applied to both.

## 4. Patch format decision

| Format | Mechanism | Ambiguity risk | Token cost | Evidence |
|---|---|---|---|---|
| whole file | rewrite everything | none (100 % format compliance) | O(file) output + O(file) later input | aider "whole": 100 % well-formed but 80.5 % correct (gemini-exp-1206) and slower/costlier [21][22] |
| SEARCH/REPLACE (exact string) | match `old`, substitute `new` | unique-match required ⇒ near zero | O(hunk) | aider diff 84.2 %/99.2 % [22]; Anthropic demands exactly one match else error [6] |
| unified diff (git-style) | `@@` hunk headers + context | line-number/context drift | O(hunk), cheaper to *emit* than SEARCH/REPLACE for big files | GPT-4 "oracle-collapsed": 1,116/2,292 applied (48.7 %), 684 of them (61 %) only via the repair pass; 26–54 % apply-rate across models with BM25 retrieval [28] |
| udiff (aider's relaxed diff) | `@@ ... @@`, no line numbers | low; model-friendly | O(hunk) | GPT-4 Turbo laziness 20 %→61 % (12→4 placeholder cases); dropping the high-level prompting increased bad patches 30–50 % [23] |
| Codex `apply_patch` | freeform grammar (`*** Begin Patch`) | grammar-validated | O(hunk) | `type:"custom"` tool, raw text payload, verified then applied [18][19] |
| AST / tree-sitter edit | replace node by kind/range | requires correct node id | O(node) + index build | 165 k symbols/7,386 files in 16.6 s measured; incremental reuse via `TSInputEdit`, `ERROR`/`MISSING` recovery [41] |
| LSP `workspace/applyEdit` | server-computed `TextEdit[]` | high for lexical targets | +6 %→+118 % tokens on localization | location-only LSP failed 3/4 multi-file renames (misses comments/strings); index-warmed+inline-text version still didn't close it [35] |
| adaptive / delegated | choose format per edit, or put editing in a clean-context sub-agent | — | −17.9 % inference cost | SWE-Edit +2.1 pp resolve; GRPO-trained 8B editor picking find-replace vs whole-file gains +12.5 pp edit success [32]; Diff-XYZ: no universal winner [33]; AdaEdit: structure-aware formats trade off differently by task [34] |

**Decision.** Aiden ships three mutation paths and picks deterministically:

1. `edit(path, old, new)` — **default**. Exact string, unique match required, no line numbers in
   the payload, `old` is verified against the *read-cached* content.
2. `edit(..., all=true)` for mechanical multi-site replacements in one file (bounded by a printed
   diff, not by trust).
3. `write(path, content)` for new files and for files under ~150 lines, or after a failed `edit`
   recovery (aider's own escalation, and the "whole" format's 100 % compliance makes it the safe
   retry lane) [21][22].

Rejected: unified-diff as the primary interface (application fragility [28]); LSP edits as a
retrieval-driven path (precision ≠ recall ceiling; comments/strings [35]); AST-range edits as a
model-facing format (node ids are a bookkeeping tax the model pays badly — same reasoning aider
gives for avoiding line-number arithmetic [1]). LSP/AST stay *internal*: validation, rename
fan-out checking, and diagnostics surfaced as a lint gate.

Validation gate (non-negotiable, worth 3.0 pp alone [1]):

```python
# aiden/tools/edit.py  — order matters: cheap checks first, all failures are data
def apply_edit(st: ReadState, path: str, old: str, new: str, all_: bool) -> ToolResult:
    cur = read_bytes(path)
    if st.sha256(cur) != st.hashed:  # 2.2 GB/s measured
        return err("stale_view", window=st.window(cur))  # force a re-read, don't guess
    hits = find_all(cur.text, old)  # aider's exact-first ladder:
    if len(hits) == 0:  # exact → whitespace-tolerant →
        return err("not_found", candidates=fuzzy_nearest(cur.text, old, lo=0.9, hi=1.1))
    if len(hits) > 1 and not all_:  # ≥0.8 similarity fallback; retry w/ only the
        return err("ambiguous", n=len(hits), lines=[line_of(h) for h in hits])  # failed block
    out = substitute(cur.text, hits, new)
    if diag := lint(path, out):  # ruff/tree-sitter parse, SWE-agent's
        return err("lint", blocks=diag, reverted=True)  # flake8 F821/E111/E999 subset [1]
    write_bytes(path, out)
    st.update(path)  # refresh own-write state or the next
    return diff_hunks(cur.text, out, max_lines=60)  # edit falsely trips the guard
```

Every failure string is a *retry instruction with data attached*: count, candidate lines, and the
current window. aider's feedback message and SWE-agent's three-part linting error (error, what the
edit would have looked like, original file) exist because "Failed Edit Recovery" loops account for
23.4 % of unresolved instances [1][26].

## 5. Search strategy decision

Measured latency (this machine, §3.1) and published latency/recall:

| Method | Warm latency / cost | Recall-precision profile | Verdict |
|---|---|---|---|
| `rg -l` (files with matches) | 0.20 s over 104 MB, 650 tok | high recall, lexical noise, no semantics | **primary** |
| `rg -n` (line:content) | 0.175 s, 1.2 k tok | + location, still lexical | secondary, `head=200` |
| `rg -n -C3` | 6.7× tokens | "too many results per hit" is the classic confusion | explicit opt-in only |
| `grep -rn` | 1.094 s (6.3× `rg`) | — | never |
| ripgrep on kernel tree (upstream) | 0.082 s vs 0.674 s GNU grep (10.69×), 536 matches | — | [40] |
| tree-sitter/AST symbol index | 16.6 s build/7,386 files; 0.25 µs lookup; 0.9 ms reparse | precise *definitions*, misses strings/comments; outline = 4 % of a full read | **repo map + `symbols` plugin** |
| universal-ctags | binary search over sorted tags [42]; ⚠️ macOS ships a ctags without `-R` (measured: `illegal option`) | same shape as above, cheaper build | optional backend; don't assume the binary |
| LSP (`definition`, `references`) | process warm-up + per-symbol round-trip | precise, **+6 %→+118 % tokens** on localization; ~50 % spontaneous use on reference tasks; can't exceed the recall set by model thoroughness | **validation only** (diagnostics, rename fan-out) [35]; compiler/LSP signals double as deterministic process rewards when training editors [36] |
| embeddings / dense retrieval | index + query cost; offline Recall@20 0.703 vs BM25 0.445 on one community bench [43]; up to +80 % MAP/MRR for repo file ranking [37] | helps *file* ranking; in-agent gains unstable, can *hurt* (retrieved similar code reduced quality by up to 15 % [39]) | **out of core**; optional MCP server |
| agent-driven filesystem search (grep+glob+read) | as above | frontier agents beat prior long-context SOTA by 17.3 % by *navigating files* instead of semantic queries | the pattern we are copying [38] |

```
             ┌──────────── Aiden retrieval ladder (stop at the first rung that answers) ────────────┐
 task ──▶ 1. glob/ls            ~30 ms,  <100 tok   "which files exist?"
           2. grep -l           ~0.2 s,  ~0.6 k tok "which files mention X?"
           3. grep -n head=200  ~0.2 s,  ~1.2 k tok "where in them?"
           4. read(offset,400)  ~1 ms,   ~1-4 k tok "the code, with line numbers"
           5. symbols/outline   ~1 ms cached, ~1.3 k tok  big-file navigation w/o paging  (deferred tool)
           6. sub-agent sweep   clean window, returns 1-2 k tok  [52]  — for "audit every caller"
           7. dense/ctags/LSP   only via plugin, only when rungs 1-5 fail twice
             └─ always: report timing + "+N more" so the model can re-prompt itself ─┘
```

**Decision.** Lexical-first, just-in-time. Keep paths and one-line references in context and pull
content on demand [7]. Maintain one *structural* artifact — a repo map of definitions ranked by
reference/PageRank-style score under a hard token budget (aider's `--map-tokens` default is 1024,
a target not a cap) [24][25] — because at 4 % of a full read the outline is the cheapest context we
can buy. Do not build an embedding index for a single local repo: measured `rg` latency (0.175 s) is
already below the model's own round-trip, so retrieval speed is not our bottleneck — retrieval
*verbosity* is. LSP enters only as a diagnostics/validation channel.

## 6. Read-state caching and mtime validation

Per-session state, keyed by absolute path:

```python
@dataclass(slots=True)
class ReadState:  # one entry per file the model has seen
    path: str
    digest: str  # sha256 of bytes at last read  (2.2 GB/s measured)
    mtime_ns: int
    size: int
    window: tuple[int, int]  # lines actually admitted to context
    partial: bool  # whole-file read was budget-truncated
```

Rules, each bought with a citation:

1. **Read-before-edit** for existing files (Claude Code's `Edit` precondition) [14]; new files exempt.
2. **Validate by content hash, not mtime.** mtime is a coarse proxy: a formatter or a
   timestamp-resolution artifact either creates a false conflict or hides a real one; third-party
   implementations of this guard hit exactly that caveat [27][14]. Hashing is 2.2 GB/s measured.
3. **Refresh the entry after your own write**, or the next edit falsely reports a stale view
   (a real bug class in read-before-edit designs) [27].
4. **Never re-send bytes the model already has.** On a repeat read of a covered window return
   `unchanged since your last read (lines 1-400 of 15,716)` — a ~1.4–33 k token saving per
   repeat, and an append-only 1-token event rather than an edit to history (§3.3).
5. **Invalidate by watcher, coalesced.** aider watches with `watchfiles` and keys its rendered
   repo-map context by `(path, lines_of_interest, mtime)`, re-parsing only what changed; it checks
   `.aiderignore`'s mtime at most once per second [27]. Copy the coalescing, not the polling.
6. **Keep the index warm off the critical path**: one `ast`/tree-sitter parse per file, 0.9 ms
   median / 6.2 ms for 756 KB measured, so a post-edit reparse of the touched file is invisible.

## 7. Parallel tool calls

Semantics differ by provider and must be handled explicitly: OpenAI defaults `parallel_tool_calls`
to `true` and returns an *array* — "these were requested in one turn", not "run in this order", and
every call needs a matching result id; Anthropic enables it by default on modern models and disables
it with `tool_choice.disable_parallel_tool_use` [45][11]. Anthropic's own guidance: parallelise
independent read-only calls to cut latency, serialise anything with side effects or shared state [11].

Aiden's policy, stated in the tool dispatcher rather than the prompt:

- `read`, `grep`, `glob`, `web_fetch`, `bash` with `readonly=true` ⇒ concurrent, cap 8, stable
  result ordering so the transcript stays reproducible.
- `edit`, `write`, and any `bash` that may mutate ⇒ serialised; a mutating call queued in the same
  batch as a read of the same path downgrades to "read first, then mutate".
- One failing call in a batch retries only that call (per-call `tool_use_id` / `call_id`) [11][45].
  And if the provider's `stop_reason == "max_tokens"` cut a tool call in half, never execute the
  partial — re-issue the request with a larger output budget [12].

The arithmetic favours this strongly: a turn's cost is dominated by model decode + round-trip
(seconds), while tools cost 1.7–185 ms measured (§3.1). Four greps in one turn ≈ 0.7 s of tools;
four turns ≈ 12–40 s of model time. But parallel batches also *concentrate* output tokens in one
message, so the per-turn budget must be enforced across the batch (e.g. 8 results sharing one
16 k-token cap), not per call — otherwise a parallel fan-out is just a bigger context bomb.

Counter-evidence worth respecting: SWE-agent's system prompt forbids multiple commands per turn
("If you'd like to issue two commands at once, PLEASE DO NOT DO THAT!") because its shell interface
has one shared cursor (open file + cwd) [1]. That is an argument for *stateless* tool arguments
(absolute paths, no hidden cursor — also Anthropic's absolute-path rule [6]), not against batching.

## 8. Sandboxing and exec timeouts

| Harness | FS | Net | Notes |
|---|---|---|---|
| Codex CLI [20] | macOS Seatbelt `sandbox-exec`; Linux **bubblewrap-first** (read-only FS + re-mounted writable roots, userns/pidns), Landlock as legacy fallback | netns + TCP→unix→TCP bridge; seccomp blocks new `AF_UNIX`/`socketpair` once active to stop proxy bypass; `PR_SET_NO_NEW_PRIVS` | modes `read-only` / `workspace-write` (net off by default) / `danger-full-access` |
| Claude Code [16][17] | Seatbelt / bwrap, writes scoped to workspace | proxy + `allowedDomains` (wildcards, ports), `strictAllowlist`, `failIfUnavailable`, `allowUnsandboxedCommands:false` | proxy decides on SNI/hostname, no TLS inspection → domain-fronting exfil risk; `allowAppleEvents` breaks isolation; GHSA escape patched in v2.1.2 |
| Gemini CLI [44] | per-tool output caps (40 k chars) | separate | output shaping is part of the safety story |
| pi [55] | **no built-in sandbox, by design** — local trust boundary; project trust is an *input-loading* guard only | host | real isolation delegated to container/VM or a micro-VM ("Gondolin") that tool calls execute inside |

Aiden orchestrates pi and inherits that stance: **no prompt-level sandbox, real isolation by OS
boundary.** Concretely: (a) run untrusted repos inside a container/devcontainer with a read-only
mount of the workspace and no credentials except short-lived ones; (b) network off by default for
`bash`, allowlisted domains via a proxy; (c) mutating tools (`edit`/`write`) always require the
read-state guard, and self-update patches to Aiden's own prompts/skills require a *separate*
approval path — this is the lethal-trifecta case (private data + untrusted content + external
channel) that no amount of prompt hygiene fixes [46]; CaMeL's data-flow/capability design solved
77 % of AgentDojo tasks *with provable* security properties (vs 84 % for the undefended system),
which is the honest bar for "we could prevent this" [47].

Timeout policy (Claude Code's numbers are the calibration: `BASH_DEFAULT_TIMEOUT_MS=120000`,
`BASH_MAX_TIMEOUT_MS=600000`, `BASH_MAX_OUTPUT_LENGTH=30000` chars capped at 150,000, middle
truncation; `CLAUDE_CODE_WEBFETCH_DEADLINE_MS` ≈ 5 min) [14][15]:

| Tool | Default | Max | On expiry |
|---|---|---|---|
| `read`/`glob` | 5 s | 10 s | error + path (it's a filesystem stall; don't retry blindly) |
| `grep` | 10 s | 30 s | partial results + "N files scanned" → model narrows the pattern |
| `edit`/`write` | 5 s | 5 s | fail *loudly* (never half-applied: write temp + atomic rename) |
| `bash` | 30 s | 600 s | keep streaming partial stdout, promote to background, return poll id |
| `web_fetch` | 15 s | 60 s (vs 300 s in Claude Code [15]) | fail with URL + attempt count; Aiden is not a crawler |

Long-lived processes must be first-class (`background` + poll), because SWE-agent had to ban
interactive commands entirely in its shell (`python`, `vim`) and paid for it in awkward
reproducer-scripts workarounds [1][2]. Timeouts should be ≥100× the 1.7 ms spawn floor, and note
that path assumptions break: `/bin/true` does not exist on macOS (measured; it is `/usr/bin/true`).

## Instrumentation plan

Model: tool calls are `Action → Executor → Observation`, and the append-only event stream is at once
the agent's memory and its observability surface — instrument that stream, do not bolt on a second
log [54]. Store one row per tool call, in the session DB (SQLite; `~/.aiden/metrics.sqlite3`),
joined to the session JSONL that already carries provider usage — pi's session format records
`usage.{inputTokens,outputTokens,cacheRead,cacheWrite}` plus `cost{...}` per assistant message, and
a nested `usage` for tools that themselves do LLM work (our `web_fetch` extractor) [55].

| # | Metric | Definition | Target / why |
|---|---|---|---|
| 1 | tokens/task | Σ in+out per episode, split cached/uncached | comparability: Epoch runs ≈200–300 k tok/task [51] |
| 2 | tool-calls/turn | calls ÷ assistant turns | >1 = batching working; ~0 = model serialising needlessly |
| 3 | result tokens/call, p50/p90/max | per tool | guardrail vs §3.1 baselines (`grep` ≈1.2 k, `read` ≤4 k) |
| 4 | **wasted-output ratio** | `1 − |referenced_output_bytes| / |emitted_output_bytes|`, where "referenced" = any line/`path:line`/identifier from a result that appears in a *later* model call before the file state changes | proxies "did we pay for text nobody read"; our own number, no external benchmark exists |
| 5 | edit first-pass rate | edits applied without a 0/ambiguous/stale/lint failure | the 23.4 % Failed-Edit-Recovery failure class, measured locally [1] |
| 6 | re-read ratio | repeat reads of an already-covered window | the caching win in §6, in % |
| 7 | truncation rate + spill read-back | calls truncated; how often the spilling file was later read | tells whether caps are too tight or too loose |
| 8 | wall vs model time | `Σ tool_duration` vs turn count × TTFT+decode | separates "slow tools" from "slow loop" |

Instrumentation shape: a decorator on the single tool dispatcher, emitting an OpenTelemetry
`execute_tool {gen_ai.tool.name}` INTERNAL span with `gen_ai.operation.name="execute_tool"`,
duration covering retries, error via `error.type`; note the released spec has no official
`gen_ai.execute_tool.duration` metric yet (proposal open-telemetry/semantic-conventions-genai#249),
so derive from spans [48]. Bench harness reporting follows HAL's premise — accuracy, cost, and
scaffold together, Pareto frontiers, not an accuracy rank [49] — plus Terminal-Bench's insistence on
pinning env/timeouts/CPU-RAM in the record [50], because Anthropic measured up to 6 % of failures in
one agentic setup as infrastructure noise, not capability [53].

Because Aiden self-updates its own tooling, add a **change gate**: a 20-task regression suite
(repos on this machine, verifiable outcomes), run 3× per arm, accepted only if
`edit first-pass rate` and `resolved` do not fall while `tokens/task` and `wasted-output ratio`
improve — Anthropic's tool-evaluation loop (multi-step tasks with verifiable outcomes; track errors,
parameter mistakes, call counts, latency, tokens; read whole transcripts; keep a
near-100 %-pass regression suite alongside a hill-climb suite) is directly reusable for tool changes,
not just model changes [3].

## Implications for Aiden

**Copy, concretely**

- pi's truncation utilities and budgets as our starting numbers — `DEFAULT_MAX_BYTES` 50 KB,
  `DEFAULT_MAX_LINES` 2000, `truncateHead`/`truncateTail`, spill-to-file with the path in the
  message, and 2,000-char tool-result trimming at compaction time (`docs/extensions.md`,
  `docs/compaction.md`) [55]. Then tighten `read` to 400 lines / 16 KB: the 171× whole-file ratio
  and the 90 k-token grep both argue our ceiling is ~2.5× pi's.
- SWE-agent's *observation hygiene*: numbered windows, "summarized" (not iterative) search output,
  an explicit message for empty results, collapsed old observations, and the lint-gated edit with
  the three-part error payload [1][2].
- Anthropic's edit contract: absolute paths always, unique-match-or-error, tool descriptions that
  carry the operating rules (no internet, persistent cwd, output control) [6].
- aider's escalation ladder for failed SEARCH blocks (exact → whitespace-tolerant → ≥0.8 similarity
  → targeted retry feedback) and its ~1 k-token PageRank repo map [24][26][57].
- Codex's *freeform* tool idea, later: a grammar-checked `apply_patch` avoids JSON-escaping bugs,
  and it is the right shape once we need multi-file atomic changes [18][19].

**Skip, and why**

- Embedding-indexed code search in the core (0.175 s `rg` beats any round-trip; agent-native search
  outperforms published long-context SOTA by 17.3 % [38]; retrieved "similar code" can *hurt* by 15 % [39]).
- LSP as a retrieval tool (+6 %→+118 % tokens; misses comments/strings ⇒ 3/4 rename failures [35]);
  keep LSP/tree-sitter for validation and rename fan-out only.
- Unified-diff as the model-facing patch syntax (48.7 % pre-repair application [28]).
- Iterative/paged search UIs that require `next`/`prev` bookkeeping (12.0 % vs 18.0 % [1]).
- A bespoke in-process sandbox that implies safety (pi's reasoning: partial isolation is
  misunderstood as a boundary while still depending on host shell/credentials [55]).
- Interactive subprocesses in `bash` (needs `background`+poll instead) [1].

**Build order**: (1) tool dispatcher with budgets + OTel spans + SQLite metrics; (2) `read`/`grep`/
`glob`/`bash` with spill; (3) `ReadState` + `edit`/`write` with the validation ladder; (4) repo-map
outline (4 %-of-read cost) behind a 1 k-token budget; (5) the 20-task regression suite and the
change gate; (6) deferred tools (symbols, task/sub-agent, note) — after we can measure them.

## Open questions

1. **Is exact-string editing still right for 2026 frontier models?** The best edit-format evidence
   is 2024-era aider/SWE-bench data [21][22][28]; SWE-Edit's adaptive result [32] suggests format
   choice matters less than *delegation*. Needs a first-party Aiden ablation (edit first-pass rate
   + tokens/task on our suite).
2. **Wasted-output ratio has no published definition or baseline.** Metric #4 is invented here;
   its line-provenance heuristic may over-count incidental reuse. Calibrate against hand-labelled
   transcripts.
3. **Where does the budget cap actually hurt?** No public measurement of middle- vs head- vs
   tail-truncation effects on downstream task success; Gemini CLI's 20/80 split [44] and Claude
   Code's middle truncation [15] are engineering judgement calls.
4. **Does a repo map still pay in a 1 M-token window with cheap cache reads?** aider's 1,024-token
   default is a 2023 number [24]; nobody has re-measured structural prefill vs pure agentic
   discovery under 2026 caching prices (0.1× reads [13] may make *everything* prefill cheap).
5. **How many parallel calls before compaction pressure beats the latency win?** Batch-wide output
   caps are unmeasured in every harness we could read.
6. **Token accounting across providers is not comparable.** SWE-agent explicitly warns token counts
   differ by tokenizer [1]; mixed-provider sessions make tokens/task a within-Aiden metric only.
7. **Are the live SWE-bench per-instance costs ($0.07–$0.75 [29]) inference-only?** The board
   reports model cost; sandbox/runtime cost is excluded, so our cost/task will be higher.
8. **Tool-schema cost when pi + MCP servers are loaded.** Our own session shows a large MCP
   surface; need to measure Aiden's actual `schema_tokens` and decide when to move to deferred
   loading [10] or code-execution-over-MCP [5].

## Sources

- [1] SWE-agent: Agent-Computer Interfaces Enable Automated Software Engineering (NeurIPS 2024) — Tables 1/3, App. B — https://papers.neurips.cc/paper_files/paper/2024/file/5a7c947568c1b1328ccc5230172e1e7c-Paper-Conference.pdf (arXiv: https://arxiv.org/abs/2405.15793)
- [2] SWE-agent docs: `docs/background/aci.md` — https://swe-agent.com/latest/background/aci/
- [3] Anthropic Engineering: Writing effective tools for AI agents — using AI agents — https://www.anthropic.com/engineering/writing-tools-for-agents
- [4] Anthropic Engineering: Building effective agents — https://www.anthropic.com/engineering/building-effective-agents
- [5] Anthropic Engineering: Code execution with MCP — https://www.anthropic.com/engineering/code-execution-with-mcp
- [6] Anthropic Engineering: Raising the bar on SWE-bench Verified with Claude 3.5 Sonnet — https://www.anthropic.com/engineering/swe-bench-sonnet
- [7] Anthropic Engineering: Effective context engineering for AI agents — https://www.anthropic.com/engineering/effective-context-engineering-for-ai-agents
- [8] Anthropic: Managing context on the Claude Developer Platform (context editing + memory tool) — https://claude.com/blog/context-management
- [9] Claude Platform Docs: Compaction (server-side, 150 k default trigger) — https://platform.claude.com/docs/en/build-with-claude/compaction
- [10] Claude Platform Docs: Tool search tool (`defer_loading`) — https://platform.claude.com/docs/en/agents-and-tools/tool-use/tool-search-tool
- [11] Claude Platform Docs: Parallel tool use — https://platform.claude.com/docs/en/agents-and-tools/tool-use/parallel-tool-use
- [12] Claude Platform Docs: Stop reasons and fallback (`max_tokens`, partial `tool_use`) — https://platform.claude.com/docs/en/build-with-claude/handling-stop-reasons
- [13] Claude Platform Docs: Pricing & prompt caching multipliers — https://docs.anthropic.com/en/docs/about-claude/pricing
- [14] Claude Code Docs: Tools reference (Read PARTIAL view, Grep offsets, WebFetch extractor) — https://code.claude.com/docs/en/tools-reference
- [15] Claude Code Docs: Environment variables (`BASH_*TIMEOUT`, `BASH_MAX_OUTPUT_LENGTH`, `CLAUDE_CODE_WEBFETCH_DEADLINE_MS`) — https://code.claude.com/docs/en/env-vars
- [16] Claude Code Docs: Configure the sandboxed Bash tool — https://code.claude.com/docs/en/sandboxing
- [17] Anthropic advisory GHSA-ff64-7w26-62rf (settings.json sandbox escape) — https://github.com/anthropics/claude-code/security/advisories/GHSA-ff64-7w26-62rf
- [18] openai/codex: `codex-rs/prompts/templates/apply_patch_tool_instructions.md` — https://github.com/openai/codex/blob/main/codex-rs/prompts/templates/apply_patch_tool_instructions.md
- [19] openai/codex: `codex-rs/core/src/tools/handlers/apply_patch.rs` — https://github.com/openai/codex/blob/main/codex-rs/core/src/tools/handlers/apply_patch.rs
- [20] openai/codex: `codex-rs/linux-sandbox/README.md` (bwrap, seccomp, proxy bridge) — https://github.com/openai/codex/blob/main/codex-rs/linux-sandbox/README.md
- [21] aider docs: Edit formats (whole / diff / diff-fenced / udiff) — https://aider.chat/docs/more/edit-formats.html
- [22] aider: Code editing leaderboard (percent completed correctly / percent using correct edit format) — https://aider.chat/docs/leaderboards/edit.html
- [23] aider blog: Unified diffs make GPT-4 Turbo 3X less lazy — https://aider.chat/2023/12/21/unified-diffs.html
- [24] aider docs: Repository map (`--map-tokens`, tree-sitter + PageRank) — https://github.com/Aider-AI/aider/blob/main/aider/website/docs/repomap.md
- [25] aider blog: Building a better repository map with tree-sitter — https://aider.chat/2023-10-22/repomap.html
- [26] aider: `aider/coders/editblock_coder.py` (exact-first ladder, ≥0.8 fuzzy, retry feedback) — https://github.com/Aider-AI/aider/blob/main/aider/coders/editblock_coder.py
- [27] aider: `aider/repomap.py` (mtime-keyed context), `aider/repo.py` (ignore-cache mtime ≤1/s), `aider/watch.py` (watchfiles) — https://github.com/Aider-AI/aider/blob/main/aider/repomap.py ; read-before-edit/stale-view guard discussion — https://octomind.run/blog/why-we-built-octofs
- [28] SWE-bench paper (arXiv:2310.06770) — generated-patch application rates, Table 14 repair pass — https://arxiv.org/abs/2310.06770
- [29] SWE-bench Leaderboards (bash-only board incl. mini-SWE-agent, avg cost/instance) — https://www.swebench.com/
- [30] mini-SWE-agent docs & repo — https://mini-swe-agent.com/ ; https://github.com/SWE-agent/mini-swe-agent
- [31] Agentless: Demystifying LLM-based Software Engineering Agents (32.00 %, $0.70 on SWE-bench Lite) — https://arxiv.org/abs/2407.01489
- [32] SWE-Edit: Rethinking Code Editing for Efficient SWE-Agent (+2.1 pp, −17.9 % cost; adaptive find-replace/whole-file) — https://arxiv.org/abs/2604.26102
- [33] Diff-XYZ: A Benchmark for Evaluating Diff Understanding — https://arxiv.org/abs/2510.12487
- [34] To Diff or Not to Diff? Structure-Aware and Adaptive Output Formats for Code Editing (Findings of ACL 2026) — https://aclanthology.org/2026.findings-acl.1483/
- [35] Does a Language Server Save Tokens for Coding Agents? (arXiv:2608.13568; +6 %→+118 % tokens, 3/4 rename failures) — https://arxiv.org/abs/2608.13568 ; replication repo — https://github.com/Poytr1/lsp-vs-grep-token-study
- [36] Reinforcement Learning from Compiler and Language Server Feedback (arXiv:2510.22907) — https://arxiv.org/abs/2510.22907
- [37] Repository-level Code Search with Neural Retrieval Methods (up to +80 % MAP/MRR) — https://arxiv.org/abs/2502.07067
- [38] Coding Agents are Effective Long-Context Processors (+17.3 %; BM25/dense not consistently helpful) — https://arxiv.org/abs/2603.20432
- [39] An Exploratory Study of Code Retrieval Techniques in Coding Agents (preprints.org 202510.0924; 108 k vs 117 k tokens lexical vs LSP; preprints.org bot-walls automated fetchers — abstract verified via search index) — https://www.preprints.org/manuscript/202510.0924 ; What to Retrieve for Effective Retrieval-Augmented Code Generation (retrieved similar code up to 15 % worse) — https://arxiv.org/abs/2503.20589
- [40] BurntSushi/ripgrep README benchmark table (0.082 s vs 0.674 s, 536 matches) — https://github.com/BurntSushi/ripgrep/blob/master/README.md
- [41] tree-sitter docs: Advanced Parsing (`TSInputEdit`, incremental reuse), Syntax (`ERROR`/`MISSING`) — https://tree-sitter.github.io/tree-sitter/using-parsers/3-advanced-parsing.html
- [42] universal-ctags: `readtags(1)` (binary search over sorted tags; stdin path inefficient) and `ctags-faq(7)` — https://docs.ctags.io/en/stable/man/readtags.1.html ; https://docs.ctags.io/en/latest/man/ctags-faq.7.html
- [43] Agent Retrieval Bench (dense Recall@20 0.703 vs BM25 0.445; structural RepoMap best at an 8 k-token budget; community methodology, unverified) — https://github.com/eyuansu62/agent-retrieval-bench ; ast-grep vs Semgrep runtimes on 75 k LOC (no precision/recall reported) — https://github.com/codemod/benchmark
- [44] Gemini CLI settings (`tools.truncateToolOutputThreshold` 40,000 chars) — https://github.com/google-gemini/gemini-cli/blob/main/docs/cli/settings.md ; head/tail 20/80 rationale — https://github.com/google-gemini/gemini-cli/discussions/8297 (mirrored settings doc: https://app.unpkg.com/%40google/gemini-cli-core%400.33.0/files/dist/docs/cli/settings.md)
- [45] OpenAI API reference: Chat Completions / Responses tool-call array semantics, `parallel_tool_calls` — https://platform.openai.com/docs/guides/function-calling
- [46] Simon Willison: The lethal trifecta for AI agents — https://simonwillison.net/2025/Jun/16/the-lethal-trifecta/
- [47] Defeating Prompt Injections by Design (CaMeL, 77 % of AgentDojo with provable security vs 84 % undefended) — https://arxiv.org/abs/2503.18813 ; code — https://github.com/google-research/camel-prompt-injection
- [48] OpenTelemetry GenAI semantic conventions: `execute_tool` spans — https://github.com/open-telemetry/semantic-conventions-genai/blob/main/docs/gen-ai/gen-ai-spans.md ; metric naming proposal #249 — https://github.com/open-telemetry/semantic-conventions-genai/issues/249
- [49] HAL: Holistic Agent Leaderboard (accuracy × cost × scaffold) — https://hal.cs.princeton.edu/
- [50] Terminal-Bench 2.0 run docs (fixed timeouts/resources; env metadata) — https://www.tbench.ai/docs/run-terminal-bench-2-0
- [51] Epoch AI: How to run SWE-bench Verified in one hour on one machine (100–150 M tokens / 500 tasks; token ceilings) — https://epoch.ai/latest/swebench-docker
- [52] Anthropic Engineering: How we built our multi-agent research system (+90.2 %, ≈15× tokens, 1–2 k token sub-agent returns) — https://www.anthropic.com/engineering/multi-agent-research-system
- [53] Anthropic Engineering: Quantifying infrastructure noise in agentic coding evals (up to 6 % infra-caused failures) — https://www.anthropic.com/engineering/infrastructure-noise
- [54] OpenHands platform paper (arXiv:2407.16741) and SDK tool-system docs (Action → Executor → Observation) — https://arxiv.org/abs/2407.16741 ; https://docs.openhands.dev/sdk/arch/tool-system
- [55] pi coding-agent docs (harness Aiden orchestrates): `docs/extensions.md` §Output Truncation (50 KB / 2,000 lines, `truncateHead`/`truncateTail`, spill path); `docs/compaction.md` (16,384 `reserveTokens`, 20,000 `keepRecentTokens`, 2,000-char tool-result trim); `docs/session-format.md` (`usage`, `cost`, nested tool `usage`); `docs/security.md` (no built-in sandbox, project trust is input-loading only) — local: `/Users/harshsingh/.nvm/versions/node/v24.12.0/lib/node_modules/@earendil-works/pi-coding-agent/docs/` ; upstream https://github.com/earendil-works/pi-mono
- [56] Third-party Claude Code tool-schema dumps (Grep `head_limit` default 250, ~200-file cap; Read 25 k-token limit) — not official docs, treated as reverse-engineered — https://github.com/x1xhlol/system-prompts-and-models-of-ai-tools/blob/main/Anthropic/Claude%20Code/Tools.json ; https://github.com/anthropics/claude-code/issues/6910
- [57] aider docs: Prompt caching (`--cache-prompts`: system prompt, read-only files, repo map, chat files) — https://aider.chat/docs/usage/caching.html

*First-hand numbers in §3.1, §5 and §6 were measured on 2026-09-15 on an Apple M1 Max / macOS
14.8.7 machine against the local Python 3.11 install (7,386 `.py` files, 103.9 MB) with ripgrep
15.1.0 and a warm page cache; tokens estimated at 3.6 chars/token. Treat them as reproducible
order-of-magnitude inputs, not benchmarks.*
