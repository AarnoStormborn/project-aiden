"""Compaction: keeping a long run inside the model's context window.

Two mechanisms, in this order ([architecture §6], [r01] §context):

    estimate -> compact? -> send

Estimating is deliberate. No provider here exposes a tokenizer we can call per model, and the
decision compaction makes is coarse — *is the next request near the window?* — so a cheap estimator
that is right about the boundary is more useful than an expensive one that is exactly right. The
dangerous direction is under-counting: a request that overflows fails the whole turn, while a
request that compacts slightly early costs one summary call. Every rounding below therefore goes up.

Sources: docs/architecture/aiden-architecture.md §6; docs/research/00-reference-source-study.md §3
(the auto-compaction predicate, `reserveTokens=16384`, `keepRecentTokens=20k`); docs/research/01
§context; docs/research/09 §2 (measured evidence for the numbers, cross-checked in `config.py`).
"""

from __future__ import annotations

from .compact import (
    SUMMARY_INSTRUCTIONS,
    SUMMARY_MAX_TOKENS,
    Compaction,
    apply_compaction,
    cut_index,
    needs_compaction,
    render_for_summary,
    summarise,
    summary_prompt,
)
from .estimate import estimate_tokens, should_compact

__all__ = [
    "SUMMARY_INSTRUCTIONS",
    "SUMMARY_MAX_TOKENS",
    "Compaction",
    "apply_compaction",
    "cut_index",
    "estimate_tokens",
    "needs_compaction",
    "render_for_summary",
    "should_compact",
    "summarise",
    "summary_prompt",
]
