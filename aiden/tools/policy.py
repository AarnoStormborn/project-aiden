"""Write policy: which paths may be modified, and why not.

`research/01` §permissions describes the chain as **schema → semantics → policy → approval**, with
deny beating allow and hooks unable to upgrade a decision. This module is the *policy* step, kept
separate from the tools so Tier-2 self-update rules can be layered on later without editing every
tool (docs/architecture/aiden-architecture.md §4.5).

The rules are deliberately narrow and about *safety*, not about self-modification:

- Anything outside the working directory is refused (a coding agent is confined to its project).
- ``.git/**`` is refused: writing there corrupts the repository that is our rollback.
- A user-extensible denylist, so a project can protect generated files or vendored trees.

Self-update policy — "the Refiner cannot write code", ``config.py`` never, ``aiden/**`` only inside
a proposal worktree — belongs in a *later* layer applied on top, because it constrains the agent
modifying *itself*, not the agent modifying the user's project.
"""

from __future__ import annotations

import fnmatch
import os
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path

from .types import resolve_in_cwd

#: Refused unconditionally. Writing to .git corrupts the rollback path we depend on.
NEVER = (
    ".git/**",
    ".git",
)

#: Refused by default, overridable with AIDEN_WRITE_ALLOW.
SENSITIVE = (
    ".env",
    ".env.*",
    "*.pem",
    "*.key",
    ".aiden/**",
)


@dataclass(frozen=True, slots=True)
class Decision:
    """Outcome of the policy step."""

    allowed: bool
    reason: str = ""
    #: True when the path may be written but deserves a louder prompt (secrets, config).
    sensitive: bool = False

    @classmethod
    def allow(cls, *, sensitive: bool = False) -> Decision:
        return cls(True, sensitive=sensitive)

    @classmethod
    def deny(cls, reason: str) -> Decision:
        return cls(False, reason=reason)


@dataclass
class WritePolicy:
    """Path rules for mutating tools."""

    never: tuple[str, ...] = NEVER
    sensitive: tuple[str, ...] = SENSITIVE
    allow_extra: tuple[str, ...] = field(default_factory=tuple)

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None) -> WritePolicy:
        # os.environ is an os._Environ, not a dict[str, str]; Mapping is the honest type.
        resolved: Mapping[str, str] = os.environ if env is None else env
        extra = tuple(
            p.strip() for p in (resolved.get("AIDEN_WRITE_ALLOW") or "").split(",") if p.strip()
        )
        return cls(allow_extra=extra)

    def check(self, raw_path: str, cwd: Path) -> tuple[Path | None, Decision]:
        """Resolve and classify a path for writing.

        Returns the resolved path (or ``None`` when refused) plus the decision, so the caller can
        report *why* rather than a bare failure.
        """
        resolved, refusal = resolve_in_cwd(raw_path, cwd)
        if resolved is None:
            return None, Decision.deny(
                f"{refusal} Writes are confined to the project, so this was not applied."
            )

        relative = _relative(resolved, cwd)

        for pattern in self.allow_extra:
            if fnmatch.fnmatch(relative, pattern) or fnmatch.fnmatch(relative, f"{pattern}/**"):
                return resolved, Decision.allow()

        for pattern in self.never:
            if fnmatch.fnmatch(relative, pattern):
                return None, Decision.deny(
                    f"refused: '{relative}' is inside {pattern}, and {'.git'!r} is the rollback "
                    "path this harness depends on. Write elsewhere or do it by hand."
                )

        for pattern in self.sensitive:
            if fnmatch.fnmatch(relative, pattern):
                # Allowed, but flagged so the approval prompt can say what it is touching. Secrets
                # are exactly the case where a rubber-stamped "yes" is most expensive.
                return resolved, Decision.allow(sensitive=True)

        return resolved, Decision.allow()

    def describe(self) -> str:
        parts = [f"never: {', '.join(self.never)}", f"sensitive: {', '.join(self.sensitive)}"]
        if self.allow_extra:
            parts.append(f"allowed: {', '.join(self.allow_extra)}")
        return " · ".join(parts)


def _relative(path: Path, cwd: Path) -> str:
    try:
        return str(path.relative_to(cwd.resolve()))
    except ValueError:
        return str(path)
