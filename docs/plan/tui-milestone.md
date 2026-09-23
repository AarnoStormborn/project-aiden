# TUI milestone — streaming renderer, snapshot-tested

**Status: core complete and live.** 79 TUI tests (of 230 total), `ruff`/`mypy`/`pytest` green.
`aiden ask --tui on` renders a live run inline; `--tui auto` (default) turns it on only for a
real terminal.

## What shipped

```
aiden/tui/theme.py    45 colour tokens, both palettes, ASCII fallbacks, monochrome mapping
aiden/tui/model.py    Cell union + reducer over harness events (pure, no I/O)
aiden/tui/render.py   Cell -> Lines via rich segments; the escape-hatch boundary
aiden/tui/stream.py   stable-vs-tail split, newline commit boundaries, table/fence holdback
aiden/tui/writer.py   differential line writer; the only module that knows escape sequences
aiden/tui/driver.py   inline driver: erase -> commit to scrollback -> repaint the tail
aiden/tui/plain.py    linear, escape-free renderer for pipes and screen readers
aiden/tui/golden.py   record events -> replay -> snapshot at three widths
tests/tui/golden/     the snapshots themselves (turn + plain, at 80/94/120 columns)
```

The layering is one-directional and is the reason the UI is testable headless:

```
loop.py --emits--> Event --reduce--> Transcript(cells) --render--> Lines --> writer
```

`loop.py` was not modified. It already emitted events instead of printing, so the TUI replaced
`ConsoleSink` rather than the loop — which is what the architecture promised.

## Mechanisms, each traceable to the research

| Mechanism | Why | Source |
|---|---|---|
| Commit at newline boundaries; keep the partial line in the tail | committing mid-line repaints a line the user is already reading | `markdown_stream.rs` |
| Hold back pipe tables and unterminated fences | a new row can reflow every column, so the table renders once, whole | `table_holdback.rs` |
| One line per tick, draining under pressure, hysteresis on exit | avoids gear-flapping when the model outruns the renderer | `chunking.rs` `EXIT_HOLD` |
| `CSI ?2026h/l` around every frame, capability-gated and overridable | no partial frames; Textual had to disable it per-terminal, so the gate is the feature | pi-tui, Codex |
| Never clear; move to the first changed row and erase | clearing destroys scrollback | pi-tui's three render cases |
| Finalized lines go to real scrollback | selection, tmux copy and Cmd-F keep working | `insert_history.rs` |
| Status is glyph + style, never colour alone | survives greyscale and `NO_COLOR` | spec §Colour tokens |
| `plain` shares the cell model | one model, two renderers; a screen reader surface is required, not a downgrade | spec §Accessibility |

## Bugs the tests and the harness found

The snapshot-first approach paid for itself immediately — every one of these was invisible until
something rendered or replayed a frame:

1. **`compute_damage` ignored shrinking frames.** Removing lines returned "pure append", so
   deleted rows would have stayed on screen. Now a shrink is a rewrite.
2. **`flush()` permanently finalized the stream.** Called on every non-text event, it made `tail`
   return `""` forever — the live region was silently never painted.
3. **`tail`/`stable` ignored `committed_chars`.** Text already written to scrollback was still
   reported as mutable, so it could be repainted after being committed.
4. **A `RUNNING` tool cell was committed at start.** Its output and final status could never
   appear. Committing is now gated on the cell being final.
5. **`deepseek`-style ASCII fallbacks made ok and error identical** (`[x]` for both) and
   monochrome mapped both to `bold`. Status was colour-only, which the spec forbids. Now
   `[ok]`/`[err]` and `bold`/`bold underline`.
6. **`Theme.from_env(env)` ignored its own `env`** for glyph selection, so an explicit environment
   could pick a colour mode and an ASCII mode that disagreed.
7. **The golden round-trip dropped a trailing blank line** (`rstrip("\n")` removed the frame's
   own last line as well as the file terminator).
8. **The harness itself caught two stale claims in `theme.py`**: a docstring asserting "40 tokens"
   when the tuple held 45, and a reference to `tests/tui/test_theme.py` that did not exist. The
   test now exists, and the docstring no longer carries a number that can drift.

## What testing in a real terminal found

The golden frames caught layout bugs; running the thing in a pty caught a second, worse class.
Every item below was invisible in tests and obvious within seconds on screen.

1. **The UI emitted no colour at all.** 74 SGR sequences in a full run, all of them `\x1b[0m`.
   The renderers called `theme.style(...)`, embedded it in a `rich.Text`, and then read `.plain`,
   which strips the style — so the entire 45-token palette was decorative. No test noticed,
   because the only colour assertion covered `markdown_block`, which renders through rich and
   therefore worked. There is now a test that asserts the *cell* renderers emit distinct codes.
2. **Assistant prose was clipped, not wrapped.** Every answer longer than the terminal ended
   mid-sentence with an ellipsis, which reads as a model failure rather than a display choice.
   Prose now wraps; tool output wraps too, because a clipped line of code is unusable and the
   ellipsis only tells you something is missing.
3. **Tool output was a 160-character preview presented as the whole result.** `ToolCallFinished`
   carried `output_preview`, so the transcript showed a fragment with no marker and nothing to
   expand. The event now carries the full output; the renderer caps what it *shows* and counts
   what it hides (`… 154 more (e to expand)`).
4. **Every turn's prose accumulated into one cell.** `_close_assistant()` only flipped a status
   flag instead of clearing the open cell, so text after a tool call — and text in the next turn —
   appended to the same cell. A turn's answer rendered *above* that turn's own marker. Cell
   boundaries are now real, and `tests/tui/test_model.py` pins the ordering.
5. **Ctrl-C printed a traceback.** Raising `KeyboardInterrupt` from a SIGINT handler unwinds
   through asyncio's selector, so the exception escapes `run_until_complete` rather than the
   awaiting coroutine — the `except` in `_run` never saw it. SIGINT is now left to the default
   handler and caught around `asyncio.run`, and `abort()` marks every in-flight cell aborted so
   partial work is committed instead of erased with the live region.
6. **The run summary printed twice.** The TUI drew a summary cell and the CLI also printed its
   own report to stderr. The CLI report is now suppressed when the TUI rendered the run.
7. **The README advertised `AIDEN_MAX_TURNS=10` while the code used 12.** A live run answered a
   question *about* the config and volunteered the discrepancy. `tests/test_docs_consistency.py`
   now compares the README's environment table against `config`, because that is the second time
   a number in prose drifted from the code.

## Measured

| Budget (spec) | Measured |
|---|---|
| steady state ≤ 400 bytes/frame | **0 bytes** when nothing changed (asserted) |
| ≤ 2 KB/frame during a normal stream | **~254 bytes/frame** over a live run (3,043 bytes / 12 frames) |
| no full-screen clear | 0 occurrences of `CSI 2J` in a full coloured run (asserted) |
| synchronized frames | 6 start / 6 end markers in one run; balanced |
| colour actually emitted | 127 SGR sequences, 9 distinct codes, in a 6 KB run |
| render deterministically | same events → identical frames (asserted) |
| frames fit their width | no line exceeds the frame width at 80/94/120 (asserted) |

## Interactive session (aiden/tui/repl.py, keys.py)

`aiden tui` opens a session: ask, follow up, `/help`, `/model`, `/turns`, `/cost`,
`/transcript`, `/sessions`, `/keys`, `/clear`, `/quit`. Slash commands are handled locally, so a
typo never becomes a billed request.

The keymap (`keys.py`) is pi-shaped: **actions are names** (`thinking.toggle`), never keys, with
several chords each, overridable from `~/.aiden/keys.toml`, and an *explicit* precedence rule —
context-local bindings shadow global ones, which is how `Ctrl+P` stays editor history in the prompt
while `Ctrl+D` quits everywhere. `conflicts()` reports chords bound twice in one context rather
than silently letting the last registration win, and `essential_actions_present()` refuses to let
a config unbind quit or interrupt. Chords are normalized so `Ctrl+O`, `ctrl+o`, `o+ctrl`, `esc`
and `escape` are one binding.

Deliberate scope: **input happens between runs, not during one.** While a run streams, the prompt
is not reading, so the live region owns the cursor and there is no contention. Steering a run
mid-flight needs the editor and driver coordinated through `patch_stdout`; claiming it now would
mean an input path that silently drops keystrokes.

## Markdown rendering

Answers were showing their own source: `**bold**`, `|---|` tables and ``` fences reached the
transcript verbatim, because `render.markdown_block` and `render.syntax_block` existed and were
never called. `aiden/tui/render.py` now renders at **block boundaries**: streaming text stays plain
in the live region (re-parsing markdown per delta is the expensive path), and a block is rendered
as markdown the moment a blank line closes it. A single block with no blank line is bounded by
`LIVE_LINE_CAP` so a wall of text cannot repaint the whole answer every frame.

Four bugs surfaced while wiring this, all in the same place — the boundary between the reducer and
the renderer:

- **The reducer was writing the driver's render state.** `_close_assistant` set
  `cell.committed = cell.text`, so the driver believed the cell had already been emitted and
  finalized text silently never reached scrollback. `committed` means "already written to
  scrollback" and belongs to the driver; the reducer now only sets status.
- **The commit unit was a line.** Half a paragraph cannot be markdown-rendered, so commits are now
  whole blocks.
- **Assistant bookkeeping counted lines**, which cannot survive markdown rendering changing the
  line count. It now tracks characters.
- **A single line with no blank line after it is not a complete block**, so a one-line answer stays
  live until the segment settles rather than committing immediately. Correct, and worth knowing
  when reading the tests.

Also fixed while using it: grep emitted absolute paths (long, wrapping badly, and not what `read`
expects) because the prefix stripping used the *search root*, which fails when the root is a file;
and the wrap-up notice fired on turn 1 of a short run, telling the agent to answer before it had
read anything.

## Still not built

- **Alt-screen overlays**: `review`, `transcript` pager, `sessions`, approval prompts. The
  `AltScreen` discipline (transient, prints its document back on exit) is specified but the
  overlays themselves are not implemented; `/transcript` and `/sessions` currently print inline.
- **Steering a run mid-flight** (`send`/`follow_up` semantics), including the queue UI.
- **Mouse, Kitty keyboard protocol, OSC 8 hyperlinks**: capability probe and degradation.
- **Syntax highlighting off the main thread**: `render.syntax_block` exists and is unused; the
  worker-thread discipline is specified but not wired, so code fences in answers are wrapped but
  not highlighted.
- **Session resume in the UI**: `aiden sessions show` prints a transcript and the reducer could
  replay a log into cells, but `aiden tui --resume <id>` does not exist yet.

## Open questions

- **Should the live region commit incrementally for long answers?** It does for stable lines, but
  a fully-committed answer loses the ability to re-wrap on resize. The spec accepts this; it may
  not survive contact with a 140-column terminal that gets narrowed mid-answer.
- **Where does the thinking cell belong?** It currently sits inline in the transcript before the
  prose. The spec suggests a folded cell; whether it should also appear in the rail is undecided.
- **Is `plain` the a11y story, or does Ink-style summary output need to be emitted *alongside*
  the TUI?** Currently a screen-reader user must choose `--tui off`, which is honest but blunt.
