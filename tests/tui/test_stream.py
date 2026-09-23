"""Streaming boundaries: what may be committed, and when.

These are the mechanisms research/06 calls the actual hard part, so they get the most precise
tests: exact offsets for the stable/tail split, and the commit cadence's hysteresis.
"""

from __future__ import annotations

from aiden.tui.stream import Chunker, StreamController, holdback_start, split

# --------------------------------------------------------------------------- split


def test_incomplete_last_line_stays_in_the_tail():
    stable, tail = split("first line\nsecond line\npartial")
    assert stable == "first line\nsecond line\n"
    assert tail == "partial"


def test_text_without_newline_is_entirely_tail():
    stable, tail = split("nothing committed yet")
    assert stable == ""
    assert tail == "nothing committed yet"


def test_empty_input_is_empty():
    assert split("") == ("", "")


def test_trailing_newline_commits_everything():
    stable, tail = split("done\n")
    assert stable == "done\n"
    assert tail == ""


def test_table_rows_are_held_back_until_the_table_ends():
    """A new row can reflow every column, so the whole table stays mutable."""
    text = "intro\n| a | b |\n| 1 | 2 |\n"
    stable, tail = split(text)
    assert stable == "intro\n", "the table must not be committed"
    assert tail == "| a | b |\n| 1 | 2 |\n"


def test_table_is_released_once_a_blank_line_closes_it():
    text = "| a | b |\n| 1 | 2 |\n\nread next\n"
    stable, tail = split(text)
    # The blank line closes the table, and the final line ends in a newline, so all is stable.
    assert "| a | b |" in stable
    assert stable == text
    assert tail == ""


def test_unterminated_fence_is_held_back_from_its_opening():
    text = "before\n```python\nx = 1\n"
    stable, tail = split(text)
    assert stable == "before\n"
    assert tail == "```python\nx = 1\n"


def test_closed_fence_commits():
    text = "```python\nx = 1\n```\nafter\n"
    stable, tail = split(text)
    assert "```python" in stable
    assert stable == text
    assert tail == ""


def test_two_fences_do_not_confuse_the_state_machine():
    text = "```\na\n```\ntext\n```\nb\n"
    stable, tail = split(text)
    # The second fence is still open, so everything from it is mutable.
    assert stable == "```\na\n```\ntext\n"
    assert tail == "```\nb\n"


def test_holdback_start_returns_none_for_plain_text():
    assert holdback_start("plain\nlines\n") is None


def test_holdback_offset_points_at_the_first_table_row():
    text = "intro\n| a |\n| b |\n"
    assert holdback_start(text) == len("intro\n")


# --------------------------------------------------------------------------- chunker


def test_smooth_gear_releases_one_line_per_tick():
    chunker = Chunker(smooth_batch=1, catch_up_threshold=8)
    assert chunker.next_batch(3) == 1
    assert not chunker.catching_up


def test_gear_shifts_up_when_the_model_outruns_the_renderer():
    chunker = Chunker(smooth_batch=1, catch_up_threshold=8)
    assert chunker.next_batch(12) == 12
    assert chunker.catching_up


def test_gear_exit_is_held_to_avoid_flapping():
    """chunking.rs holds the exit for EXIT_HOLD ticks so gears do not oscillate."""
    chunker = Chunker(smooth_batch=1, catch_up_threshold=8, exit_hold=3)
    chunker.next_batch(12)  # enter catch_up
    assert chunker.catching_up

    for _ in range(2):
        chunker.next_batch(1)
        assert chunker.catching_up, "exited catch-up too early"

    chunker.next_batch(1)
    assert not chunker.catching_up


def test_zero_pending_does_not_enter_catch_up():
    chunker = Chunker()
    assert chunker.next_batch(0) == 0
    assert not chunker.catching_up


# --------------------------------------------------------------------------- controller


def test_controller_separates_stable_and_tail():
    controller = StreamController()
    controller.push("line one\nline two")
    assert controller.stable == "line one\n"
    assert controller.tail == "line two"


def test_controller_commits_only_complete_blocks():
    """Blocks, not lines: committing half a paragraph would render it twice, in two forms."""
    controller = StreamController()
    controller.push("first paragraph\n")
    assert controller.take_blocks() == "", "an open paragraph is not committable"
    assert "first paragraph" in controller.uncommitted

    controller.push("\nsecond paragraph\n")
    assert controller.take_blocks() == "first paragraph\n\n"
    # The next block is complete but not yet released by the cadence, so it is still uncommitted
    # while `tail` (the open, unterminated line) is empty.
    assert controller.uncommitted == "second paragraph\n"
    assert controller.tail == ""


def test_controller_respects_cadence_under_load():
    controller = StreamController(Chunker(smooth_batch=1, catch_up_threshold=4))
    controller.push("a\n\nb\n\nc\n\n")
    assert controller.take_blocks() == "a\n\n", "smooth gear releases one block"
    assert controller.take_blocks() == "b\n\n"

    # The model outruns the renderer: pending blocks exceed the threshold, so catch up and drain.
    controller.push("d\n\ne\n\nf\n\ng\n\nh\n\n")
    drained = controller.take_blocks()
    assert drained == "c\n\nd\n\ne\n\nf\n\ng\n\nh\n\n"


def test_flush_commits_everything_including_the_partial_line():
    """End of turn: nothing more is coming, so the partial line is no longer mutable."""
    controller = StreamController()
    controller.push("done\nand partial")
    assert controller.flush() == "done\nand partial"
    assert controller.tail == ""


def test_finalize_makes_the_partial_line_stable():
    controller = StreamController()
    controller.push("text\nmore")
    controller.finalize()
    assert controller.tail == ""
    assert controller.stable == "text\nmore"


def test_take_blocks_is_empty_when_no_complete_block_exists():
    controller = StreamController()
    controller.push("partial")
    assert controller.take_blocks() == ""

    controller.push(" still partial\n")
    assert controller.take_blocks() == "", "one newline does not close a block"


def test_take_rest_does_not_finalize_the_stream():
    """Regression: finalizing here made `tail` permanently empty, so nothing ever painted."""
    controller = StreamController()
    controller.push("complete\npartial")
    assert controller.take_rest() == "complete\npartial"

    controller.reset()
    controller.push("second segment\nmore")
    assert controller.tail == "more", "the live region must work again after a tool call"
    assert not controller._finalized


def test_flush_finalizes_but_take_rest_does_not():
    controller = StreamController()
    controller.push("a")
    controller.take_rest()
    assert controller.tail == ""  # nothing buffered, but not finalized
    controller.push("b")
    assert controller.tail == "b"

    controller.flush()
    assert controller._finalized
    assert controller.tail == ""
