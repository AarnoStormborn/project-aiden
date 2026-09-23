"""Keyboard model: namespaced actions, multiple chords, documented precedence.

research/06 §Keyboard model prescribes pi's shape — namespaced action ids with several default
chords each, overridable in config, and a *documented* precedence rather than whichever binding
happened to be registered last.

Two ideas carry the design:

- An action is a **name** (``thinking.toggle``), never a key. Keys are data, so a user can
  rebind without the code knowing, and tests can assert on intent instead of on ``"t"``.
- Resolution is **contextual**. The same chord means different things depending on whether the
  prompt or the transcript has focus, and that conflict must be resolved by an explicit rule:
  context-local bindings win over global ones, and within a context the first chord listed wins.
  pi hit this exact case — ``Ctrl+P`` is editor history when the editor is focused, model cycling
  otherwise — so it is a real conflict, not a hypothetical one.

Everything here is pure: chord strings in, action names out.
"""

from __future__ import annotations

import tomllib
from collections.abc import Iterator
from dataclasses import dataclass, field
from pathlib import Path

#: Focus contexts, in resolution order.
GLOBAL = "global"
EDITOR = "editor"
TRANSCRIPT = "transcript"

#: action -> chords, per context. The first chord listed is the one shown in help.
DEFAULT_BINDINGS: dict[str, dict[str, tuple[str, ...]]] = {
    EDITOR: {
        "prompt.submit": ("enter",),
        "editor.newline": ("alt+enter", "ctrl+j"),
        "editor.history.prev": ("up",),
        "editor.history.next": ("down",),
        "editor.clear": ("ctrl+u",),
        # Deliberately context-local: when the prompt is focused, Ctrl+P is history, not model
        # cycling. This is the conflict the spec calls out.
        "editor.history.search": ("ctrl+p",),
    },
    TRANSCRIPT: {
        "transcript.follow": ("end", "shift+g"),
        "transcript.top": ("home", "g"),
        "transcript.page_down": ("pagedown", "ctrl+f"),
        "transcript.page_up": ("pageup", "ctrl+b"),
        "thinking.toggle": ("t",),
        "tool.expand": ("e",),
        "session.list": ("ctrl+g",),
        "transcript.open": ("ctrl+o",),
        "review.open": ("d",),
        "transcript.plain": ("[",),
    },
    GLOBAL: {
        # Esc and Ctrl-C interrupt the run; at an idle prompt the REPL treats them as cancel/quit.
        "run.interrupt": ("escape", "ctrl+c"),
        "app.help": ("f1",),
        "app.quit": ("ctrl+d", "ctrl+q"),
        "app.palette": ("ctrl+k",),
    },
}

#: Chords that must never be rebound away, because they are the only way out.
ESSENTIAL: dict[str, str] = {
    "app.quit": "without a quit binding the UI can trap the user",
    "run.interrupt": "an interrupt must always be reachable",
}

_MODIFIER_ORDER = ("ctrl", "alt", "shift", "super", "meta")

#: Tokens that are never the key, plus aliases for the same physical modifier.
_MODIFIER_ALIASES = {
    "meta": "alt",  # most terminals send Alt for Meta
    "cmd": "super",
    "command": "super",
    "opt": "alt",
    "option": "alt",
}
_KNOWN_MODIFIERS = set(_MODIFIER_ORDER) | set(_MODIFIER_ALIASES)

#: Key-name aliases. Terminals, config files and prompt_toolkit each spell these differently, and
#: a binding that silently never fires because a user wrote "esc" instead of "escape" is a bug.
_KEY_ALIASES = {
    "esc": "escape",
    "return": "enter",
    "cr": "enter",
    "pgup": "pageup",
    "pgdn": "pagedown",
    "del": "delete",
    "ins": "insert",
    "bs": "backspace",
    "spacebar": "space",
}


def normalize_chord(chord: str) -> str:
    """Canonical chord form, so ``Ctrl+O``, ``ctrl+o`` and ``o+ctrl`` are one binding.

    The key is identified by *absence* from the modifier set rather than by position, because
    notation like ``o+ctrl`` appears in the wild and positional parsing silently turned it into a
    distinct binding that never fired.
    """
    parts = [p.strip().lower() for p in chord.replace("_", "+").split("+") if p.strip()]
    if not parts:
        return ""

    modifiers = [p for p in parts if p in _KNOWN_MODIFIERS]
    others = [p for p in parts if p not in _KNOWN_MODIFIERS]

    if others:
        key = others[-1]
        # Extra non-modifier tokens are treated as modifiers rather than dropped.
        modifiers.extend(others[:-1])
    else:
        # Degenerate: modifiers only ("ctrl+shift"). Keep the last as the key.
        key = parts[-1]
        modifiers = parts[:-1]

    key = _KEY_ALIASES.get(key, key)
    canonical = sorted({_MODIFIER_ALIASES.get(m, m) for m in modifiers if m != key})
    ordered = [m for m in _MODIFIER_ORDER if m in set(canonical)]
    ordered.extend(sorted(set(canonical) - set(_MODIFIER_ORDER)))
    return "+".join([*ordered, key])


def _flatten_actions(
    context: str, node: dict, prefix: str = ""
) -> Iterator[tuple[str, tuple[str, ...]]]:
    """Yield ``(dotted_action, chords)`` from a possibly-nested TOML table."""
    for key, value in node.items():
        name = f"{prefix}.{key}" if prefix else key
        if isinstance(value, dict):
            yield from _flatten_actions(context, value, name)
            continue
        if isinstance(value, str):
            raise ValueError(
                f'{context}.{name} must be a list of chords, e.g. [{value!r}] → ["{value}"]'
            )
        if not isinstance(value, list) or not value:
            raise ValueError(f"{context}.{name} must be a non-empty list of chords")
        yield name, tuple(str(c) for c in value)


@dataclass
class Keymap:
    """Action bindings for each focus context."""

    bindings: dict[str, dict[str, tuple[str, ...]]] = field(default_factory=dict)

    @classmethod
    def default(cls) -> Keymap:
        return cls(
            {
                context: {action: tuple(chords) for action, chords in actions.items()}
                for context, actions in DEFAULT_BINDINGS.items()
            }
        )

    @classmethod
    def load(cls, path: Path | None = None) -> Keymap:
        """Defaults, with a user TOML file layered on top.

        Format mirrors the defaults::

            [transcript]
            thinking.toggle = ["t", "ctrl+t"]
        """
        keymap = cls.default()
        if path is not None and path.is_file():
            keymap.apply_file(path)
        return keymap

    def apply_file(self, path: Path) -> None:
        """Layer a user TOML file over the defaults.

        TOML parses a dotted key as a *nested table*, so ``thinking.toggle = ["t"]`` arrives as
        ``{"thinking": {"toggle": [...]}}``. Action ids contain dots by design, so the nested
        form is flattened back into dotted names here; an earlier version rejected the file with a
        confusing "thinking must be a non-empty list of chords".
        """
        raw = tomllib.loads(path.read_text())
        for context, actions in raw.items():
            if context not in (GLOBAL, EDITOR, TRANSCRIPT):
                raise ValueError(
                    f"unknown keymap context {context!r}; expected one of "
                    f"{GLOBAL}, {EDITOR}, {TRANSCRIPT}"
                )
            if not isinstance(actions, dict):
                raise ValueError(f"[{context}] must be a table of action = [chords]")
            for action, chords in _flatten_actions(context, actions):
                self.bindings.setdefault(context, {})[action] = chords

    # ------------------------------------------------------------------ query

    def resolve(self, chord: str, context: str = TRANSCRIPT) -> str | None:
        """The action bound to ``chord`` in ``context``.

        Precedence: the context's own bindings first, then global. A context-local binding is
        allowed to shadow a global one, which is how Ctrl+P stays editor history in the prompt.
        """
        wanted = normalize_chord(chord)
        if not wanted:
            return None
        for scope in (context, GLOBAL):
            for action, chords in self.bindings.get(scope, {}).items():
                if any(normalize_chord(c) == wanted for c in chords):
                    return action
        return None

    def chords_for(self, action: str) -> tuple[str, ...]:
        """Every chord bound to an action, context-local entries first."""
        out: list[str] = []
        for scope in (EDITOR, TRANSCRIPT, GLOBAL):
            for candidate, chords in self.bindings.get(scope, {}).items():
                if candidate == action:
                    out.extend(chords)
        return tuple(out)

    def conflicts(self) -> list[tuple[str, str, str]]:
        """Chords bound to more than one action in the same context.

        Duplicates are not fatal (the first listed wins) but they are almost always a mistake, so
        they are reported rather than silently resolved.
        """
        found: list[tuple[str, str, str]] = []
        for context, actions in self.bindings.items():
            seen: dict[str, str] = {}
            for action, chords in actions.items():
                for chord in chords:
                    key = normalize_chord(chord)
                    if key in seen and seen[key] != action:
                        found.append((context, key, f"{seen[key]} vs {action}"))
                    seen.setdefault(key, action)
        return found

    def essential_actions_present(self) -> list[str]:
        """Actions in :data:`ESSENTIAL` that a user config has unbound."""
        missing: list[str] = []
        for action in ESSENTIAL:
            if not self.chords_for(action):
                missing.append(action)
        return missing

    def help_rows(self) -> list[tuple[str, str]]:
        """``(chord, action)`` for a help screen, de-duplicated and stable-ordered."""
        rows: list[tuple[str, str]] = []
        seen: set[str] = set()
        for context in (EDITOR, TRANSCRIPT, GLOBAL):
            for action in sorted(self.bindings.get(context, {})):
                for chord in self.bindings[context][action]:
                    key = normalize_chord(chord)
                    if key in seen:
                        continue
                    seen.add(key)
                    rows.append((chord, action))
        return rows
