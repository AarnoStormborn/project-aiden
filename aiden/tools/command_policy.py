"""Command policy: which shell commands may run without asking.

A shell is the most dangerous tool in the box: it can write, delete, fetch, and spawn. The only thing
a policy can *prove* about a shell command is what its leading words are, so that is what this
decides — everything else asks.

`research/01` §permissions notes Codex is the only surveyed harness with a declarative policy
language (`prefix_rule` in Starlark, with `match`/`not_match` load-time tests). This is a small
version of that idea, with the same load-time validation, because a rule that silently matches the
wrong thing is worse than no rule.

Two rules carry more weight than the allowlist itself:

1. **Unprovable means ask.** Shell metacharacters make the effect of a command undecidable by prefix
   — ``cat a > b`` starts with an allowed word and writes a file. Any metacharacter forces approval.
2. **The allowlist is a floor.** User rules can only *add* read-only prefixes; they can never grant
   approval to something the built-in policy would refuse. Deny beats allow, and a hook cannot upgrade
   a permission.
"""

from __future__ import annotations

import re
import shlex
from dataclasses import dataclass, field

#: Substrings whose presence makes a command undecidable by prefix. Anything here means "ask".
#: Checked before any prefix match, deliberately: ``cat x > y`` must not pass as ``cat``.
METACHARACTERS = (
    ";",
    "&&",
    "||",
    "|",
    "`",
    "$(",
    ">",
    ">>",
    "<",
    "<<",
    "&",
    "\n",
)


@dataclass(frozen=True, slots=True)
class PrefixRule:
    """A read-only command prefix, with examples checked at load time.

    ``match`` and ``not_match`` exist so a wrong rule fails loudly at startup. A rule that
    accidentally matches everything would otherwise look like it worked.

    ``deny_tokens`` exists because a prefix cannot express "unless a later flag makes it
    destructive". ``find`` reads; ``find . -delete`` does not, and no prefix test can tell them
    apart. ``not_match`` is emphatically *not* the place for this: a prefix rule matches by prefix,
    so listing a destructive command there makes the rule lie about itself — which is exactly what
    the load-time validation caught.
    """

    prefix: tuple[str, ...]
    match: tuple[str, ...] = ()
    not_match: tuple[str, ...] = ()
    deny_tokens: tuple[str, ...] = ()

    def applies(self, words: list[str]) -> bool:
        if len(words) < len(self.prefix):
            return False
        return words[: len(self.prefix)] == list(self.prefix)

    def blocks(self, words: list[str]) -> str:
        """The deny token present in ``words``, or ``""``.

        Matches ``--output=file`` as well as a bare ``--output``: flags are routinely written with
        an inline value, and an exact comparison silently misses the most common spelling.
        """
        if not self.deny_tokens:
            return ""
        for token in words:
            for denied in self.deny_tokens:
                if token == denied or token.startswith(f"{denied}="):
                    return token
        return ""

    def validate(self) -> str:
        """Return a complaint, or ``""`` when the rule behaves as its examples say."""
        for example in self.match:
            if not self.applies(example.split()):
                return f"rule {self.prefix} claims to match {example!r} but does not"
        for example in self.not_match:
            if self.applies(example.split()):
                return f"rule {self.prefix} claims not to match {example!r} but does"
        return ""


#: Commands that only read. Not exhaustive by design — the cost of omission is one approval prompt,
#: and the cost of a wrong addition is an unattended write.
DEFAULT_RULES: tuple[PrefixRule, ...] = (
    PrefixRule(("ls",), match=("ls", "ls -la src"), not_match=("lsof",)),
    PrefixRule(("cat",), match=("cat README.md",), not_match=("catx",)),
    PrefixRule(("head",)),
    PrefixRule(("tail",)),
    PrefixRule(("wc",)),
    PrefixRule(("file",)),
    PrefixRule(("stat",)),
    PrefixRule(("tree",)),
    PrefixRule(("grep",)),
    PrefixRule(("rg",)),
    PrefixRule(("fd",)),
    PrefixRule(("find",), deny_tokens=("-delete", "-exec", "-execdir", "-ok")),
    PrefixRule(("git", "status")),
    # --output writes a file, which is a write in a read-only-looking command.
    PrefixRule(("git", "diff"), deny_tokens=("--output",)),
    PrefixRule(("git", "log"), deny_tokens=("--output",)),
    PrefixRule(("git", "show"), deny_tokens=("--output",)),
    # `git branch` lists, but -d/-D/-m/--delete rewrite refs.
    PrefixRule(
        ("git", "branch"),
        deny_tokens=("-d", "-D", "-m", "-M", "-c", "-C", "--delete", "--move", "--force"),
    ),
    PrefixRule(
        ("git", "remote"),
        deny_tokens=("add", "remove", "rm", "rename", "set-url", "prune", "update"),
    ),
    PrefixRule(("git", "blame")),
    PrefixRule(("git", "ls-files")),
    PrefixRule(("git", "rev-parse")),
    PrefixRule(("git", "describe")),
    PrefixRule(("pytest", "--collect-only")),
    PrefixRule(("uv", "run", "pytest", "--collect-only")),
    # A parse check is read-only by construction and is the cheapest useful gate on generated code.
    PrefixRule(("python", "-m", "py_compile")),
)

#: Tokens that are destructive in any context, as a safety net for rules added later. Deliberately
#: short: a broad list is worse than none, because `grep -r` and `ls -f` are ordinary read-only
#: flags, and a gate that cries wolf gets bypassed. Command-specific destructiveness belongs in a
#: rule's ``deny_tokens``.
DANGEROUS_TOKENS = ("-delete", "-exec", "-execdir", "-ok")


@dataclass
class CommandPolicy:
    """Decides whether a shell command may run without approval."""

    rules: tuple[PrefixRule, ...] = DEFAULT_RULES
    extra_rules: tuple[PrefixRule, ...] = field(default_factory=tuple)

    def validate(self) -> list[str]:
        """Complaints about the configured rules. Empty means the rules behave as documented."""
        problems: list[str] = []
        for rule in (*self.rules, *self.extra_rules):
            complaint = rule.validate()
            if complaint:
                problems.append(complaint)
        return problems

    # ------------------------------------------------------------------ decision

    def is_read_only(self, command: str) -> bool:
        """Whether ``command`` can be shown to only read."""
        if not command.strip():
            return False
        if self._has_metacharacter(command):
            return False
        words = _words(command)
        if not words:
            return False
        if any(token in DANGEROUS_TOKENS for token in words):
            return False
        for rule in (*self.rules, *self.extra_rules):
            if rule.applies(words) and not rule.blocks(words):
                return True
        return False

    def reason(self, command: str) -> str:
        """Why the command needs approval, phrased for a human reading the prompt.

        The most specific explanation wins: naming the rule and the flag that blocked it tells the
        user what to change, where "not a known read-only command" would not.
        """
        if self._has_metacharacter(command):
            return (
                "contains a shell metacharacter, so its effect cannot be determined from its "
                "leading words"
            )
        words = _words(command)
        if not words:
            return "empty command"
        for rule in (*self.rules, *self.extra_rules):
            if rule.applies(words):
                blocked = rule.blocks(words)
                if blocked:
                    return f"'{' '.join(rule.prefix)}' with {blocked} is not read-only"
        if any(token in DANGEROUS_TOKENS for token in words):
            return "contains a flag that can delete or execute"
        return f"'{words[0]}' is not a known read-only command"

    def _has_metacharacter(self, command: str) -> bool:
        # `>` is checked as a bare token too, so `a > b` and `a>b` are both caught.
        return any(
            (token in command if token not in {">", "<"} else _has_redirect(command))
            for token in METACHARACTERS
        )


def _has_redirect(command: str) -> bool:
    """True when the command redirects, without tripping on `->`, `=>`, or `2>&1`-style quoting."""
    stripped = _strip_quoted(command)
    return bool(re.search(r"(^|[^-\d])[<>]", stripped))


def _strip_quoted(command: str) -> str:
    """Remove quoted spans so a `>` inside a string literal is not read as a redirect."""
    out: list[str] = []
    quote = ""
    for char in command:
        if quote:
            if char == quote:
                quote = ""
            continue
        if char in {"'", '"'}:
            quote = char
            continue
        out.append(char)
    return "".join(out)


def _words(command: str) -> list[str]:
    try:
        return shlex.split(command)
    except ValueError:
        # Unbalanced quotes: not parseable, so not provable.
        return []


def load_rules(raw: object) -> tuple[PrefixRule, ...]:
    """Build rules from config: a list of ``{prefix: [...], match: [...], not_match: [...]}``."""
    if not isinstance(raw, list):
        return ()
    rules: list[PrefixRule] = []
    for entry in raw:
        if not isinstance(entry, dict):
            continue
        prefix = entry.get("prefix")
        if not isinstance(prefix, list) or not prefix:
            raise ValueError(f"a command rule needs a non-empty prefix list, got {entry!r}")
        rules.append(
            PrefixRule(
                prefix=tuple(str(word) for word in prefix),
                match=tuple(str(x) for x in (entry.get("match") or ())),
                not_match=tuple(str(x) for x in (entry.get("not_match") or ())),
                deny_tokens=tuple(str(x) for x in (entry.get("deny_tokens") or ())),
            )
        )
    return tuple(rules)
