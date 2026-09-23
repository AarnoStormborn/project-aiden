"""Theme invariants.

``theme.py`` promises that every token is present in both palettes and that status survives
without colour. Those promises were only in a docstring until a live harness run pointed out that
the file claimed this test existed while it did not — so the test is the deliverable here, not a
number in prose.

The docstring count is deliberately *not* asserted as a literal: it drifted (claimed 40, held 45).
Instead the test checks that the palettes match ``TOKENS`` exactly, which is the property that
actually matters.
"""

from __future__ import annotations

import pytest

from aiden.tui.theme import (
    DARK,
    LIGHT,
    TOKENS,
    Glyphs,
    Theme,
    ascii_requested,
    no_colour,
    reduced_motion,
)


def test_every_token_has_both_palette_entries():
    """A theme missing a token is a bug: render would raise mid-frame."""
    missing_dark = [t for t in TOKENS if t not in DARK]
    missing_light = [t for t in TOKENS if t not in LIGHT]
    assert not missing_dark, f"missing from DARK: {missing_dark}"
    assert not missing_light, f"missing from LIGHT: {missing_light}"


def test_palettes_define_nothing_unknown():
    unknown_dark = [k for k in DARK if k not in TOKENS]
    unknown_light = [k for k in LIGHT if k not in TOKENS]
    assert not unknown_dark, f"DARK defines untokened keys: {unknown_dark}"
    assert not unknown_light, f"LIGHT defines untokened keys: {unknown_light}"


def test_token_names_are_unique():
    assert len(TOKENS) == len(set(TOKENS))


def test_style_resolves_every_token_in_both_schemes():
    for dark in (True, False):
        theme = Theme(dark=dark, colour=True)
        for token in TOKENS:
            assert isinstance(theme.style(token), str)


def test_unknown_token_is_a_loud_error():
    theme = Theme()
    with pytest.raises(KeyError) as exc:
        theme.style("not_a_token")
    assert "not_a_token" in str(exc.value)


def test_monochrome_keeps_status_distinguishable():
    """Status must never be colour-only (research/06 §Colour tokens)."""
    theme = Theme(colour=False)
    # Errors and successes must not collapse to the same style.
    assert theme.style("error") != theme.style("success")
    assert theme.style("error") != ""
    assert theme.style("warning") != theme.style("dim")
    # Structure still reads as secondary.
    assert theme.style("dim") == "dim"


def test_monochrome_never_emits_ansi_colour_names():
    theme = Theme(colour=False)
    for token in TOKENS:
        style = theme.style(token)
        assert "grey" not in style and "cyan" not in style and "red" not in style


def test_ascii_mode_swaps_every_unicode_glyph():
    glyphs = Glyphs(ascii_mode=True)
    for value in (
        glyphs.user,
        glyphs.thinking,
        glyphs.ok,
        glyphs.error,
        glyphs.running,
        glyphs.pending,
        glyphs.warning,
        glyphs.collapsed,
        glyphs.expanded,
        glyphs.removed,
        glyphs.continuation,
        glyphs.ellipsis,
    ):
        assert value.isascii(), f"{value!r} is not ASCII"


def test_unicode_mode_uses_the_configured_glyphs():
    glyphs = Glyphs(ascii_mode=False)
    assert glyphs.user == "›"
    assert glyphs.ok == "✓"
    assert glyphs.removed == "−"


def test_ascii_requested_triggers():
    assert ascii_requested({"AIDEN_ASCII": "1"})
    assert ascii_requested({"LANG": "C"})
    assert ascii_requested({"LANG": "POSIX"})
    assert ascii_requested({"NO_COLOR": "1"}), (
        "monochrome terminals are the ones that mangle glyphs"
    )
    assert not ascii_requested({"LANG": "en_US.UTF-8"})


def test_no_colour_honours_both_variables():
    assert no_colour({"NO_COLOR": "1"})
    assert no_colour({"AIDEN_NO_COLOR": "true"})
    assert not no_colour({})


def test_reduced_motion_is_opt_in():
    assert reduced_motion({"AIDEN_ANIMATIONS": "off"})
    assert not reduced_motion({})


def test_from_env_resolves_scheme_and_colour():
    dark = Theme.from_env({"AIDEN_THEME": "dark"})
    assert dark.dark and dark.colour

    light = Theme.from_env({"AIDEN_THEME": "light"})
    assert not light.dark

    plain = Theme.from_env({"NO_COLOR": "1"})
    assert not plain.colour
    assert plain.glyphs.ascii_mode, "no-colour implies ASCII glyphs"


def test_theme_is_not_a_mutable_global():
    """Two themes must not interfere: the golden tests depend on this."""
    a = Theme(dark=True, colour=False)
    b = Theme(dark=False, colour=True)
    assert a.style("text") != b.style("text")
