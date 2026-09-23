"""Keyboard model: chords, contexts, precedence, and config overrides."""

from __future__ import annotations

from pathlib import Path

import pytest

from aiden.tui.keys import EDITOR, GLOBAL, TRANSCRIPT, Keymap, normalize_chord


def test_normalize_is_case_and_modifier_order_insensitive():
    assert normalize_chord("Ctrl+O") == "ctrl+o"
    assert normalize_chord("o+ctrl") == "ctrl+o"
    assert normalize_chord("SHIFT+ALT+enter") == "alt+shift+enter"
    assert normalize_chord("meta+x") == "alt+x", "meta must collapse to alt on terminals"
    assert normalize_chord("") == ""


def test_actions_are_names_not_keys():
    keymap = Keymap.default()
    assert keymap.resolve("t", TRANSCRIPT) == "thinking.toggle"
    assert keymap.resolve("e", TRANSCRIPT) == "tool.expand"


def test_a_chord_can_mean_different_things_per_context():
    """The documented precedence case: Ctrl+P is editor history, not model cycling."""
    keymap = Keymap.default()
    assert keymap.resolve("ctrl+p", EDITOR) == "editor.history.search"
    # It is unbound in the transcript, and must not fall through to an unrelated action.
    assert keymap.resolve("ctrl+p", TRANSCRIPT) is None


def test_context_bindings_shadow_global_ones():
    keymap = Keymap.default()
    keymap.bindings[EDITOR]["app.quit"] = ("ctrl+c",)
    assert keymap.resolve("ctrl+c", EDITOR) == "app.quit"
    # The global meaning is untouched for other contexts.
    assert keymap.resolve("ctrl+c", TRANSCRIPT) == "run.interrupt"


def test_global_bindings_apply_in_every_context():
    keymap = Keymap.default()
    for context in (EDITOR, TRANSCRIPT):
        assert keymap.resolve("ctrl+d", context) == "app.quit"


def test_normalised_lookup_finds_user_style_chords():
    keymap = Keymap.default()
    assert keymap.resolve("Ctrl+O", TRANSCRIPT) == "transcript.open"
    assert keymap.resolve("ESC", TRANSCRIPT) == "run.interrupt"


def test_unknown_chord_resolves_to_nothing():
    keymap = Keymap.default()
    assert keymap.resolve("ctrl+alt+f13", TRANSCRIPT) is None


def test_multiple_chords_per_action():
    keymap = Keymap.default()
    assert set(keymap.chords_for("run.interrupt")) == {"escape", "ctrl+c"}
    assert "end" in keymap.chords_for("transcript.follow")


def test_default_keymap_has_no_conflicts():
    assert Keymap.default().conflicts() == []


def test_conflicts_are_reported_not_silently_resolved():
    keymap = Keymap.default()
    keymap.bindings[TRANSCRIPT]["thinking.toggle"] = ("e",)
    conflicts = keymap.conflicts()
    assert conflicts, "a duplicate chord within a context must be reported"
    assert any("tool.expand" in detail for _ctx, _chord, detail in conflicts)


def test_quit_and_interrupt_cannot_be_unbound():
    keymap = Keymap.default()
    assert keymap.essential_actions_present() == []

    keymap.bindings[GLOBAL]["app.quit"] = ()
    assert "app.quit" in keymap.essential_actions_present()


def test_config_file_overrides_defaults(tmp_path: Path):
    path = tmp_path / "keys.toml"
    path.write_text(
        """
[transcript]
thinking.toggle = ["t", "ctrl+t"]
"""
    )
    keymap = Keymap.load(path)
    assert keymap.resolve("t", TRANSCRIPT) == "thinking.toggle"
    assert keymap.resolve("ctrl+t", TRANSCRIPT) == "thinking.toggle"


def test_config_file_can_add_a_chord_in_another_context(tmp_path: Path):
    path = tmp_path / "keys.toml"
    path.write_text(
        """
[editor]
app.quit = ["ctrl+q"]
"""
    )
    keymap = Keymap.load(path)
    assert keymap.resolve("ctrl+q", EDITOR) == "app.quit"


def test_config_file_rejects_an_unknown_context(tmp_path: Path):
    path = tmp_path / "keys.toml"
    path.write_text(
        """
[sideways]
thinking.toggle = ["t"]
"""
    )
    with pytest.raises(ValueError) as exc:
        Keymap.load(path)
    assert "unknown keymap context" in str(exc.value)


def test_config_file_rejects_a_bare_string(tmp_path: Path):
    path = tmp_path / "keys.toml"
    path.write_text(
        """
[transcript]
thinking.toggle = "t"
"""
    )
    with pytest.raises(ValueError) as exc:
        Keymap.load(path)
    assert "must be a list" in str(exc.value)


def test_missing_config_file_falls_back_to_defaults(tmp_path: Path):
    keymap = Keymap.load(tmp_path / "absent.toml")
    assert keymap.resolve("t", TRANSCRIPT) == "thinking.toggle"


def test_help_rows_are_unique_and_cover_every_action():
    keymap = Keymap.default()
    rows = keymap.help_rows()
    chords = [chord for chord, _ in rows]
    assert len(chords) == len(set(normalize_chord(c) for c in chords))
    actions = {action for _chord, action in rows}
    for context in (EDITOR, TRANSCRIPT, GLOBAL):
        assert set(keymap.bindings[context]) <= actions


def test_key_name_aliases_resolve_to_the_same_binding():
    """Terminals spell these differently; a silent dead binding is the worst outcome."""
    keymap = Keymap.default()
    for spelling in ("escape", "esc", "ESC", "Esc"):
        assert keymap.resolve(spelling, TRANSCRIPT) == "run.interrupt", spelling
    assert normalize_chord("esc") == normalize_chord("escape")


def test_common_key_aliases_normalise():
    assert normalize_chord("pgup") == "pageup"
    assert normalize_chord("return") == "enter"
    assert normalize_chord("CTRL+Return") == "ctrl+enter"
