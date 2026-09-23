"""The writer: damage computation, minimal output, and the flicker rules.

The steady-state budget is the important one: research/06 requires "spinner only ⇒ ≤ 400 bytes
written", which starts with **no change ⇒ zero bytes**. That is asserted directly.
"""

from __future__ import annotations

from aiden.tui.writer import (
    ERASE_TO_END,
    RESET,
    SYNC_END,
    SYNC_START,
    Damage,
    LiveRegion,
    commit,
    compute_damage,
    cursor_up,
    sync_supported,
)

# --------------------------------------------------------------------------- damage


def test_identical_frames_are_unchanged():
    damage = compute_damage(["a", "b"], ["a", "b"])
    assert damage.unchanged
    assert not damage.rewrites


def test_appended_lines_do_not_rewrite_history():
    damage = compute_damage(["a"], ["a", "b"])
    assert damage.first_changed == 1
    assert not damage.rewrites


def test_changed_line_is_a_rewrite():
    damage = compute_damage(["a", "b"], ["a", "B"])
    assert damage.first_changed == 1
    assert damage.rewrites


def test_shrinking_frame_rewrites_from_the_removed_line():
    damage = compute_damage(["a", "b", "c"], ["a", "b"])
    assert damage.first_changed == 2
    assert damage.rewrites


# --------------------------------------------------------------------------- region


def test_steady_state_writes_nothing():
    """The flicker/bandwidth budget starts here: an unchanged frame must emit zero bytes."""
    region = LiveRegion(width=80)
    region.plan(["spinner ◐"])
    assert region.plan(["spinner ◐"]) == ""


def test_changed_tail_writes_only_the_changed_region():
    region = LiveRegion(width=80)
    region.plan(["line 1", "line 2", "line 3"])
    output = region.plan(["line 1", "line 2", "line 3 CHANGED"])
    assert "line 3 CHANGED" in output
    assert "line 1" not in output, "unchanged lines must not be re-emitted"


def test_frames_are_wrapped_in_synchronized_output_by_default():
    region = LiveRegion(width=80, sync=True)
    output = region.plan(["x"])
    assert output.startswith(SYNC_START)
    assert output.endswith(SYNC_END)


def test_sync_can_be_disabled_for_broken_terminals():
    """Textual disabled synchronization in inline mode because some terminals break on it."""
    region = LiveRegion(width=80, sync=False)
    output = region.plan(["x"])
    assert SYNC_START not in output and SYNC_END not in output


def test_sync_supported_reads_the_escape_hatch():
    assert sync_supported({}) is True
    assert sync_supported({"AIDEN_SYNC": "0"}) is False
    assert sync_supported({"AIDEN_SYNC": "off"}) is False
    assert sync_supported({"AIDEN_SYNC": "1"}) is True


def test_every_written_line_resets_style():
    """A dropped frame must not leak colour into the user's shell prompt."""
    region = LiveRegion(width=80, sync=False)
    output = region.plan(["styled line"])
    assert f"styled line{RESET}\n" in output


def test_first_paint_erases_to_end_without_clearing_the_screen():
    region = LiveRegion(width=80, sync=False)
    output = region.plan(["first"])
    assert ERASE_TO_END in output
    assert "\x1b[2J" not in output, "never clear: that destroys scrollback"


def test_forget_forces_a_full_repaint():
    region = LiveRegion(width=80, sync=False)
    region.plan(["a", "b"])
    region.forget()
    assert region.plan(["a", "b"]) != ""


def test_drawn_lines_track_what_is_on_screen():
    region = LiveRegion(width=80)
    region.plan(["a", "b"])
    assert region.drawn_lines == ["a", "b"]


def test_cursor_up_is_empty_for_zero():
    assert cursor_up(0) == ""
    assert cursor_up(3) == "\x1b[3A"


def test_damage_dataclass_unchanged_helper():
    assert Damage(-1, rewrites=False).unchanged
    assert not Damage(0, rewrites=True).unchanged


# --------------------------------------------------------------------------- commit


def test_commit_emits_plain_newline_terminated_lines():
    """Committed text is the terminal's: selection, tmux copy and Cmd-F must work on it."""
    output = commit(["done", "next"])
    assert output == f"done{RESET}\nnext{RESET}\n"
    assert SYNC_START not in output
