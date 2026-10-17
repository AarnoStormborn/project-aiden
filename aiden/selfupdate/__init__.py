"""Tier-2 self-update: Aiden proposing changes to its own source, gated.

The pipeline is the product. A proposal is supplied — by a person, a script, or (later) the
learning subsystem's writer model — and this package answers one question: may it be applied?
Everything expensive and everything dangerous lives behind a gate, and the human approval at the end
is not optional.

See `docs/plan/selfupdate.md` for the decisions, and `docs/architecture/aiden-architecture.md` §4.5
for the pipeline this implements.
"""

from __future__ import annotations

from .gate import Check, Promotion, StaticGate, decide, static_gate
from .pipeline import EvalSpec, Outcome, default_evaluate, run, trailer_for
from .proposal import Proposal, load
from .scope import Violation, check, check_path

__all__ = [
    "Check",
    "EvalSpec",
    "Outcome",
    "Promotion",
    "Proposal",
    "StaticGate",
    "Violation",
    "check",
    "check_path",
    "decide",
    "default_evaluate",
    "load",
    "run",
    "static_gate",
    "trailer_for",
]
