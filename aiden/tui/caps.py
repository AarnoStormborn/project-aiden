"""Terminal capability detection and degradation.

research/06 §What great agent UIs do #15: "Capability detection with user overrides" — pi probes
OSC 8 / image protocol / truecolor and lets you force it. Ghostty's docs make the point that these
are *app-developer* features you can only assume on some terminals, so every one of them needs a
gate and an override.

Detecting by asking the terminal (DA1 / XTGETTCAP) is the thorough approach, but the spec is
explicit that probes "must not delay first paint". These are therefore resolved from the
environment, which costs nothing and covers the real cases; an explicit override always wins.

Nothing here writes escape sequences — `render` and `writer` do that.
"""

from __future__ import annotations

import os
from collections.abc import Mapping
from dataclasses import dataclass, field

#: Terminals known to implement OSC 8 hyperlinks. Kitty's own list is the authority; this covers
#: the ones a user is likely to be on, and `AIDEN_HYPERLINKS=on|off` covers everything else.
HYPERLINK_TERMINALS = {
    "ghostty",
    "wezterm",
    "kitty",
    "iterm.app",
    "iterm2",
    "konsole",
    "vscode",
    "warpterminal",
    "rio",
}

_TRUE = {"1", "true", "yes", "on"}
_FALSE = {"0", "false", "no", "off"}


def _flag(value: str | None) -> bool | None:
    if value is None:
        return None
    lowered = value.strip().lower()
    if lowered in _TRUE:
        return True
    if lowered in _FALSE:
        return False
    return None


@dataclass(frozen=True, slots=True)
class TerminalCaps:
    """What the terminal can render. Every field has a safe default."""

    #: OSC 8 clickable links.
    hyperlinks: bool = False
    #: Synchronized output (CSI ?2026), which suppresses partial frames.
    sync: bool = True
    #: 24-bit colour; False means we are limited to the 16 ANSI colours.
    truecolor: bool = True
    #: Whether the terminal is worth probing further at all (a tty).
    interactive: bool = True
    #: Capabilities the user pinned explicitly. Carried on the value so `describe()` reflects how
    #: *these* caps were decided, rather than re-reading os.environ and possibly disagreeing — the
    #: same inconsistency that `Theme.from_env` had.
    pinned: dict[str, bool] = field(default_factory=dict)

    @classmethod
    def detect(cls, env: Mapping[str, str] | None = None) -> TerminalCaps:
        env = os.environ if env is None else env
        program = (env.get("TERM_PROGRAM") or "").strip().lower()
        term = (env.get("TERM") or "").strip().lower()

        hyperlinks = _flag(env.get("AIDEN_HYPERLINKS"))
        if hyperlinks is None:
            # Unknown terminals default to *off*: a stray OSC 8 sequence is garbage in the middle
            # of the user's output, whereas a missing link is merely plain text.
            hyperlinks = program in HYPERLINK_TERMINALS or "kitty" in term

        sync = _flag(env.get("AIDEN_SYNC"))
        if sync is None:
            sync = True

        truecolor = _flag(env.get("AIDEN_TRUECOLOR"))
        if truecolor is None:
            colourterm = (env.get("COLORTERM") or "").strip().lower()
            truecolor = colourterm in {"truecolor", "24bit"} or program in HYPERLINK_TERMINALS

        pinned = {
            name: value
            for name, value in (
                ("hyperlinks", _flag(env.get("AIDEN_HYPERLINKS"))),
                ("sync", _flag(env.get("AIDEN_SYNC"))),
                ("truecolor", _flag(env.get("AIDEN_TRUECOLOR"))),
            )
            if value is not None
        }
        return cls(hyperlinks=hyperlinks, sync=sync, truecolor=truecolor, pinned=pinned)

    def overrides(self) -> dict[str, bool]:
        """Fields the user pinned explicitly, for `--show-caps` style diagnostics."""
        return dict(self.pinned)

    def describe(self) -> str:
        parts = [
            f"hyperlinks={'on' if self.hyperlinks else 'off'}",
            f"sync={'on' if self.sync else 'off'}",
            f"truecolor={'on' if self.truecolor else 'off'}",
        ]
        pinned = self.overrides()
        if pinned:
            parts.append("pinned: " + ", ".join(f"{k}={v}" for k, v in sorted(pinned.items())))
        return " · ".join(parts)


# --------------------------------------------------------------------------- OSC 8

OSC8_START = "\x1b]8;;"
OSC8_END = "\x1b]8;;\x1b\\"
ST = "\x1b\\"


def hyperlink(text: str, target: str, caps: TerminalCaps) -> str:
    """Wrap ``text`` in an OSC 8 hyperlink when the terminal supports it.

    Terminals that do not understand OSC 8 either ignore it or print the URI as garbage, which is
    why the default is off rather than on.
    """
    if not caps.hyperlinks or not text or not target:
        return text
    return f"{OSC8_START}{target}{ST}{text}{OSC8_END}"


def osc8_reset() -> str:
    """Closing sequence, emitted at the end of every line so a dropped frame cannot leak a link."""
    return OSC8_END


def file_uri(path: str, line: int | None = None) -> str:
    """A ``file://`` URI for a path, optionally pointing at a line.

    The line is not part of the URI (there is no standard fragment for it); editors that support
    ``file://host/path:line`` parse it, and those that do not still open the file.
    """
    suffix = f":{line}" if line else ""
    if path.startswith("/"):
        return f"file://{path}{suffix}"
    return f"file://{os.path.abspath(path)}{suffix}"
