# Compaction — keeping a long run inside the window

Status: **done**. Layer L5 (`docs/architecture/aiden-architecture.md` §6). This closes the last
declared-but-unimplemented path in the loop: `aiden/retry.py` classified `Action.COMPACT` for a
context overflow and nothing acted on it, so an overflow ended the run.

## 1. Why it was missing, and why that mattered

The tool surface was complete and the eval runner could measure a change, but an agent could still
die on a long task for a reason it could not fix: its own transcript. The classification existed, the
constants existed in `aiden/config.py` (`CHARS_PER_TOKEN`, `RESERVE_TOKENS`, `KEEP_RECENT_TOKENS`,
each with a source), and no code path consumed any of them. That is the recurring failure mode in
this repository — machinery that looks complete and is never reached — so it was worth naming before
writing it.

## 2. The shape, adopted from the reference harness

```
request-time:  estimate -> compact? -> send
trigger:       estimated > context_window - RESERVE_TOKENS(16_384)
cut:           walk back to KEEP_RECENT_TOKENS(20_000), never between a tool_call and its result
summary:       structured (Goal · Constraints · Progress · Key decisions · Next steps · Files)
entry:         CompactionEntry{summary, first_kept_entry_id, retained_tail, tokens_before/after}
```

The check runs **before every request**, not between turns: long runs are where context dies, and the
turn that overflows is the turn that needed the check.

## 3. Decisions

- **A `user` message carries the summary**, not an assistant one. It is information the model is
  given, not something the model said; a provider validating assistant turns should never see a
  fabricated one.
- **The classified error kind travels as a field.** `Stop.error_kind` / `Completion.error_kind` exist
  so the loop can tell an overflow (compact and resend) from an auth failure (abort). Re-parsing the
  message text is exactly what `providers/errors.py` exists to prevent.
- **The summary call is billed to the run.** It is real spend, so its usage and cost join the totals
  rather than sitting outside them; otherwise the eval's cost column would under-report.
- **`retained_tail` is recorded as `False` and not implemented.** It makes a newer compaction a
  self-contained handoff rather than an incremental one; that needs the session *tree*, which is not
  built yet. Recorded rather than silently absent.
- **Compaction is logged as an entry and shown on replay.** It is the only event that makes the
  model's context diverge from the transcript, so a reader must be able to see what replaced what.

## 4. Two guards the tests forced out

Both were found by asserting on what the model was actually sent, not on a helper's return value.

1. **Compaction could grow the context.** With `KEEP_RECENT_TOKENS` at 20k and a window smaller than
   that, walking back for the target never spent it, so the cut stayed at 0 and compaction was a
   permanent no-op — the trigger fired and the cut refused, forever. `cut_index` now takes a hard
   `tail_budget_tokens` and, when the context genuinely exceeds the effective target, elides the
   oldest messages rather than nothing.
2. **A summary can cost more than the prefix it replaces.** Replacing two small messages with a
   1500-token summary makes the next request *larger* while looking like a fix. Compaction is now
   declined when the prefix is below the summary budget, and declined again if the result would not
   shrink the context — each with a diagnostic, because a declined compaction that says nothing is
   indistinguishable from a bug.

## 5. What is not done

- **No cross-question memory.** Each question starts a fresh message list, so compaction is per-run
  and nothing is carried between runs. That is the current session model, not a compaction gap.
- **`retained_tail` / branch summaries** need the session tree (§7). Later.
- **The estimator is approximate** (4 chars/token, ceiling-rounded). It is deliberately biased to
  over-count, because under-counting means the request overflows instead of compacting early.

## 6. Acceptance

- `aiden/context/{estimate,compact}.py`; the loop compacts before a request and recovers from an
  overflow once, up to `MAX_COMPACTIONS` (3).
- 28 tests in `tests/test_compaction.py`, all offline. The loop-level ones drive the real `run_loop`
  with a fake suite and assert on the messages the model received, on the logged entry, and on replay.
- A live `aiden ask` run after the change completed normally, reading the new module to answer.
