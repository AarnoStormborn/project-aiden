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

## Measured

| Budget (spec) | Measured |
|---|---|
| steady state ≤ 400 bytes/frame | **0 bytes** when nothing changed (asserted) |
| ≤ 2 KB/frame during a normal stream | **~254 bytes/frame** over a live run (3,043 bytes / 12 frames) |
| render deterministically | same events → identical frames (asserted) |
| frames fit their width | no line exceeds the frame width at 80/94/120 (asserted) |

## Deliberately not built yet

These are the next milestone, and they attach at the same seam without touching the reducer:

- **Raw-mode input**: the editor, the namespaced keymap (`keys.py`), mode chips, steering queue.
  Today the question comes from `argv`.
- **Alt-screen overlays**: `review`, `transcript`, `sessions`, approval prompts. `AltScreen`
  discipline (transient, prints its document back on exit) is specified but unimplemented.
- **Mouse, Kitty keyboard protocol, OSC 8 hyperlinks**: capability probe and degradation.
- **A pty-based perf harness**: the byte/frame counters exist, but the spec's p50/p99 frame-time
  budgets need a real terminal. Current numbers are output-size only.
- **Syntax highlighting off the main thread**: `render.syntax_block` exists and is unused; the
  worker-thread discipline is specified but not wired.
- **Session resume in the UI**: `aiden sessions show` prints a transcript, but the TUI cannot
  reopen one yet, even though the reducer is already capable of replaying a log.

## Open questions

- **Should the live region commit incrementally for long answers?** It does for stable lines, but
  a fully-committed answer loses the ability to re-wrap on resize. The spec accepts this; it may
  not survive contact with a 140-column terminal that gets narrowed mid-answer.
- **Where does the thinking cell belong?** It currently sits inline in the transcript before the
  prose. The spec suggests a folded cell; whether it should also appear in the rail is undecided.
- **Is `plain` the a11y story, or does Ink-style summary output need to be emitted *alongside*
  the TUI?** Currently a screen-reader user must choose `--tui off`, which is honest but blunt.
