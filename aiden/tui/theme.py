"""Colour tokens and glyphs.

Two rules from research/06 §Visual identity that shape every design decision here:

1. **Status is never colour-only.** Every state carries a glyph and, where it matters, a word,
   so the UI survives greyscale; ``AIDEN_NO_COLOR``/``NO_COLOR`` yields monochrome with weight
   carrying the meaning.
2. **Every glyph has an ASCII fallback**, selected when the terminal cannot be trusted to
   render the Unicode set (``AIDEN_ASCII=1``, ``LANG=C``, or a low-colour terminal).

The tokens below are required, not optional: a theme missing one is a bug, and
``tests/tui/test_theme.py`` enforces it. (The count is asserted against ``TOKENS`` rather than
written here, because a number in a docstring goes stale the moment a token is added — this
docstring claimed 40 while the tuple held 45 until a live run of the harness caught it.)
"""

from __future__ import annotations

import os
from collections.abc import Mapping
from dataclasses import dataclass

#: Every semantic colour the UI may reference. Grouped like pi's token set
#: (research/06 §Colour tokens): core / content / diff+code / level.
TOKENS: tuple[str, ...] = (
    # core
    "text",
    "muted",
    "dim",
    "accent",
    "border",
    "border_muted",
    "border_accent",
    "success",
    "error",
    "warning",
    "scrollbar_track",
    "scrollbar_thumb",
    "selected_bg",
    # content
    "user_bg",
    "user_text",
    "tool_pending",
    "tool_ok",
    "tool_err",
    "tool_title",
    "tool_output",
    "thinking_text",
    "md_heading",
    "md_link",
    "md_code",
    "md_codeblock",
    "md_quote",
    "md_hr",
    # diff + code
    "diff_added",
    "diff_removed",
    "diff_context",
    "syn_comment",
    "syn_keyword",
    "syn_func",
    "syn_var",
    "syn_string",
    "syn_num",
    "syn_type",
    "syn_op",
    "syn_punct",
    # thinking levels, must survive greyscale
    "lvl_off",
    "lvl_min",
    "lvl_low",
    "lvl_med",
    "lvl_high",
    "lvl_xhigh",
)

#: Dark palette first (the common case), each value a rich style fragment.
DARK: dict[str, str] = {
    "text": "default",
    "muted": "grey62",
    "dim": "grey42",
    "accent": "cyan",
    "border": "grey35",
    "border_muted": "grey27",
    "border_accent": "cyan",
    "success": "green",
    "error": "red",
    "warning": "yellow",
    "scrollbar_track": "grey23",
    "scrollbar_thumb": "grey46",
    "selected_bg": "on grey23",
    "user_bg": "on grey15",
    "user_text": "bright_white",
    "tool_pending": "yellow",
    "tool_ok": "green",
    "tool_err": "red",
    "tool_title": "bold",
    "tool_output": "grey70",
    "thinking_text": "grey54",
    "md_heading": "bold bright_white",
    "md_link": "underline cyan",
    "md_code": "bright_cyan",
    "md_codeblock": "grey78",
    "md_quote": "italic grey62",
    "md_hr": "grey35",
    "diff_added": "green",
    "diff_removed": "red",
    "diff_context": "grey54",
    "syn_comment": "italic grey50",
    "syn_keyword": "magenta",
    "syn_func": "blue",
    "syn_var": "bright_white",
    "syn_string": "green",
    "syn_num": "cyan",
    "syn_type": "yellow",
    "syn_op": "grey70",
    "syn_punct": "grey54",
    "lvl_off": "grey42",
    "lvl_min": "grey54",
    "lvl_low": "cyan",
    "lvl_med": "green",
    "lvl_high": "yellow",
    "lvl_xhigh": "red",
}

LIGHT: dict[str, str] = {
    **DARK,
    "text": "black",
    "muted": "grey35",
    "dim": "grey50",
    "accent": "blue",
    "border": "grey70",
    "border_muted": "grey85",
    "border_accent": "blue",
    "tool_output": "grey30",
    "thinking_text": "grey46",
    "md_heading": "bold black",
    "md_code": "blue",
    "md_codeblock": "grey27",
    "user_bg": "on grey89",
    "user_text": "black",
    "selected_bg": "on grey85",
    "scrollbar_track": "grey89",
    "scrollbar_thumb": "grey70",
    "syn_comment": "italic grey46",
    "syn_keyword": "purple",
    "syn_func": "blue",
    "syn_type": "dark_goldenrod",
    "syn_num": "dark_cyan",
}


@dataclass(slots=True)
class Glyphs:
    """Every glyph the UI draws, with its ASCII fallback alongside."""

    ascii_mode: bool = False

    def pick(self, unicode_glyph: str, ascii_glyph: str) -> str:
        return ascii_glyph if self.ascii_mode else unicode_glyph

    @property
    def user(self) -> str:
        return self.pick("›", ">")

    @property
    def assistant(self) -> str:
        return "ai"

    @property
    def thinking(self) -> str:
        return self.pick("▌", "( )")

    @property
    def ok(self) -> str:
        # Distinct from `error`'s fallback on purpose: an ASCII terminal without colour must still
        # be able to tell success from failure. The research glyph table gave both "[x]", which
        # makes status colour-only — exactly what the spec forbids.
        return self.pick("✓", "[ok]")

    @property
    def error(self) -> str:
        return self.pick("✗", "[err]")

    @property
    def running(self) -> str:
        return self.pick("◐", "[>]")

    @property
    def pending(self) -> str:
        return self.pick("·", "[.]")

    @property
    def warning(self) -> str:
        return self.pick("⚠", "[!]")

    @property
    def collapsed(self) -> str:
        return self.pick("▸", ">")

    @property
    def expanded(self) -> str:
        return self.pick("▾", "v")

    @property
    def added(self) -> str:
        return "+"

    @property
    def removed(self) -> str:
        return self.pick("−", "-")

    @property
    def continuation(self) -> str:
        # Two-column left gutter for continuation lines (LIVE_PREFIX_COLS = 2).
        return "│" if not self.ascii_mode else "|"

    @property
    def ellipsis(self) -> str:
        return "…" if not self.ascii_mode else "..."


def _truthy(value: str | None) -> bool:
    return (value or "").strip().lower() in {"1", "true", "yes", "on"}


def ascii_requested(env: Mapping[str, str] | None = None) -> bool:
    """Whether to fall back to ASCII glyphs.

    Triggers: explicit ``AIDEN_ASCII``, a C/POSIX locale (where the Unicode set may not render),
    or ``AIDEN_NO_COLOR``/``NO_COLOR`` (monochrome terminals are the ones most likely to mangle
    box drawing).
    """
    # os.environ is an os._Environ, not a dict[str, str]; Mapping is the honest type.
    resolved: Mapping[str, str] = os.environ if env is None else env
    if _truthy(resolved.get("AIDEN_ASCII")):
        return True
    if no_colour(env):
        return True
    locale = (
        resolved.get("LC_ALL") or resolved.get("LC_CTYPE") or resolved.get("LANG") or ""
    ).upper()
    return locale in {"C", "POSIX"}


def no_colour(env: Mapping[str, str] | None = None) -> bool:
    # os.environ is an os._Environ, not a dict[str, str]; Mapping is the honest type.
    resolved: Mapping[str, str] = os.environ if env is None else env
    return _truthy(resolved.get("AIDEN_NO_COLOR")) or _truthy(resolved.get("NO_COLOR"))


def reduced_motion(env: Mapping[str, str] | None = None) -> bool:
    """``AIDEN_ANIMATIONS=off`` disables spinners and easing.

    The spec reads the OS reduce-motion flag the way Codex does; for a terminal there is no
    portable query, so an explicit setting is the honest approximation.
    """
    # os.environ is an os._Environ, not a dict[str, str]; Mapping is the honest type.
    resolved: Mapping[str, str] = os.environ if env is None else env
    return (resolved.get("AIDEN_ANIMATIONS") or "").strip().lower() in {"off", "none", "0"}


class Theme:
    """Resolves tokens to rich style fragments for a colour mode."""

    def __init__(
        self,
        *,
        dark: bool = True,
        colour: bool = True,
        ascii_mode: bool | None = None,
    ) -> None:
        self.dark = dark
        self.colour = colour
        self.glyphs = Glyphs(ascii_mode=ascii_requested() if ascii_mode is None else ascii_mode)
        self._palette = DARK if dark else LIGHT

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None) -> Theme:
        # os.environ is an os._Environ, not a dict[str, str]; Mapping is the honest type.
        resolved: Mapping[str, str] = os.environ if env is None else env
        scheme = (resolved.get("AIDEN_THEME") or "").strip().lower()
        dark = scheme != "light"
        if scheme in {"dark", "light"}:
            dark = scheme == "dark"
        # Pass the same mapping through: reading os.environ here instead would make the glyph
        # decision disagree with the colour decision when a caller supplies an explicit env.
        return cls(dark=dark, colour=not no_colour(resolved), ascii_mode=ascii_requested(resolved))

    def style(self, token: str) -> str:
        """Rich style string for a token. Unknown tokens are a programming error."""
        if token not in TOKENS:
            raise KeyError(f"unknown theme token {token!r}; add it to TOKENS and both palettes")
        if not self.colour:
            return _monochrome(token)
        return self._palette[token]


def _monochrome(token: str) -> str:
    """Map a token to emphasis when colour is unavailable.

    Meaning must survive: errors get bold+underline, success gets bold, warnings underline, and
    structure dims. Error and success must not collapse to the same style, or the UI becomes
    unreadable on a monochrome terminal.
    """
    if token in {"error", "tool_err", "diff_removed"}:
        return "bold underline"
    if token in {"success", "tool_ok", "diff_added"}:
        return "bold"
    if token in {"warning", "tool_pending"}:
        return "underline"
    if token in {"md_heading", "tool_title", "user_text"}:
        return "bold"
    if token in {"dim", "muted", "thinking_text", "border", "border_muted", "md_hr"}:
        return "dim"
    if token in {"md_quote", "syn_comment"}:
        return "italic"
    return ""
