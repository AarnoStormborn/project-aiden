# Tier-2 self-update — Aiden proposing changes to itself

Status: **pipeline done, proposer deferred**. Implements step 1-7 of `architecture` §4.5 (step 8,
tag/canary, is deferred — canary needs real usage over time and cannot be built in one sitting).

A proposal is **supplied**: a JSON file naming the files it would change, why, and what it claims to
improve. The proposer is deliberately not built here. A gate that can also write its own inputs is not
a gate, and `learning/refine.py` (M5) owns the writer model.

```bash
aiden selfupdate show  proposal.json    # what it changes, and may it be proposed at all (free)
aiden selfupdate check proposal.json    # static gate only: lint + types (free)
aiden selfupdate run   proposal.json    # the full pipeline, with approval before anything applies
```

## 1. The pipeline

```
1. scope policy   refuse before any work: what may be touched at all
2. worktree       .aiden/worktrees/<id> — evaluating never touches the user's tree
3. static gate    ruff · mypy · optionally pytest, inside that worktree
4. eval           paired runs: baseline commit vs proposal commit
5. decision       eval/report.gate() — the criteria live in exactly one place
6. approval       required, always
7. apply          write files, commit with an `Aiden-Eval` trailer
8. archive        proposal + verdict + both eval reports, rejections included
```

Ordered so the common failure costs nothing: a proposal that fails the scope policy never creates a
worktree, and one that fails lint never reaches the eval.

## 2. Two enforcements rather than requests

**`eval/**` is never writable, and neither is `aiden/config.py`, `.aiden/**`, or `.venv/`.** The eval
harness, its tasks and its oracle *are the score function*. A proposal that can edit the score
function will eventually optimise the score function, and the resulting number is worse than no
number because it looks like evidence. This is `research/03` §"Blind the improver" as code: an
instruction in a prompt is a request, a refusal in the policy is a boundary.

**Existing test files are protected; new ones are allowed.** The static gate runs pytest, so a
proposal that can weaken a test can pass by lowering the bar instead of clearing it. Adding tests is
allowed — that is how a proposal demonstrates it is right.

**Approval is not configurable.** There is no `--yes`. Tier 2 changes the code that runs every future
turn, so the eval is one lock and the human is the other. `aiden selfupdate check` uses `DenyAll` and
`apply_on_pass=False`, because asking a question on a path where nothing can be applied trains people
to approve reflexively.

## 3. The flaw found while building it

The first evaluator called `eval/runner.default_agent` for both halves. **`default_agent` runs the
loop in-process**, so it exercises whatever `aiden` package the current process imported. A task's
base commit only changes the code the *tests* execute — never the harness driving the agent. So both
halves would have measured the current checkout and been compared to each other: a prompt-only
proposal would report a delta of exactly zero, and because the gate admits `delta >= 0` (see §5), it
would have been **promoted on a measurement that never ran it**.

The evaluator now runs each half as `python -m aiden eval run --rebase <commit>` with its working
directory inside a checkout of that commit, so the candidate's own package is the one imported. Three
things carry that and none are visible in the report, so each is pinned by a test:

- the **cwd** (a checkout of the commit under test),
- the **`--rebase` commit** (the tasks must match the harness),
- **`AIDEN_EVAL_ACTIVE` stripped** from the child — the child is the grader, and `run_task` re-applies
  the marker around the agent inside it, which is where it belongs.

A fourth test proves the mechanism end to end without a model: a candidate checkout's edited prompt is
the one that comes back.

## 4. An outage is not a result

Running the pipeline for real found the `opencode-go` credential expired, so every model call failed
with HTTP 401. Both halves failed identically, every run was recorded `resolved=False`, and the gate
reported a clean **PASS**. Two bugs:

1. **A provider error counted as a model failure.** `run_task` read the loop's cost and turns without
   consulting `LoopResult.ok`, so a run that could not reach the model was graded as a task the agent
   failed to solve. `AGENTS.md` states the rule — infrastructure failures are excluded from the
   denominator — and nothing enforced it.
2. **The gate could not see it.** It compared the *delta* in infrastructure failures, and two outages
   of equal size have a delta of zero. There is now an absolute floor, `INFRA_FLOOR_PCT = 20`, on
   either side.

The lesson is the same one the eval runner taught: **a gate that cannot distinguish "measured and
failed" from "never measured" will report a pass during an outage**, which is exactly when a pass is
most misleading.

## 5. Design decisions

- **Proposals carry full file contents, not patches.** A patch has to be applied to be understood, and
  the review step needs the resulting file. The diff for review is rendered with the same `diffutil`
  the write tools use, so the reviewer sees what the approval UI would show. `None` means delete.
- **A zero delta passes.** `resolved_delta >= 0`, not `> 0`, inherited from §4.5 and pinned by a test.
  Consequence a reviewer must know: **"promoted" means "not measured as harmful", not "measured as
  better"** — which is precisely why the human approval is not optional.
- **The criteria are not restated.** `decide()` delegates to `eval/report.gate()`. A second copy would
  drift, and only one of them would be right.
- **A dirty working tree blocks the promotion commit.** Otherwise the commit sweeps up unrelated edits
  and its trailer claims an eval report for a change that was not the one evaluated — a false record,
  which is worse than no record.
- **Rejections are archived.** Rejected-because-measured is knowledge; it is what stops the same idea
  being re-proposed.

## 6. Known limitations

- **The proposer does not exist.** Nothing in this milestone writes a patch. That is `learning/refine.py`.
- **`retained_tail`-style handoff and tag/canary (step 8) are not built.** Rollback is `git revert`
  plus reload; the commit carries its own evidence in the `Aiden-Eval` trailer.
- **One seed is not evidence.** With `seeds=1` the gate can only report "not harmful". The report says
  so in words rather than implying a result.
- **The measurement inherits the task set's thinness.** Six mined tasks; a two-task trial is an
  indication, not a result, and the report prints the count next to every rate.
- **Mining couples the task set to the harness version**, so a comparison at two commits is only as
  fair as the tasks' provenance (`v0.3-eval-runner.md` §9a).
- **`max_files` / `max_new_lines` are reviewability bounds, not correctness ones.** They can refuse a
  legitimate large refactor; that is the intended direction to fail.

## 7. Acceptance

- `aiden/selfupdate/{scope,proposal,gate,pipeline}.py`, `aiden/cli/selfupdate.py` (registered in the
  dispatcher and in `--help`, pinned by a test because that is the failure mode this repository keeps
  repeating).
- `--rebase` on `aiden eval run`: the same tasks at a chosen commit, which is what makes a harness
  comparison mean anything.
- 45 offline tests in `tests/test_selfupdate.py`, plus the outage tests in `tests/test_eval.py`.
- Live: the pipeline ran end to end twice against the real outage, and refused correctly. **The
  proposal's own effect could not be measured** — the credential expired, so no live run in this
  milestone measured a model. That is recorded rather than dressed up, and re-running
  `aiden selfupdate run` after renewing the key is the first thing to do.
