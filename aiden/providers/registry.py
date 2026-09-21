"""Model resolution helpers built on top of the catalog.

Kept separate from :class:`Catalog` so lookup policy (aliases, defaults, thinking-level
parsing) can change without touching the vendored-data loader.
"""

from __future__ import annotations

import re
from collections.abc import Iterable
from dataclasses import dataclass

from .catalog import Catalog
from .errors import UnsupportedProtocolError
from .transports import TRANSPORTS
from .types import ModelInfo

# ``provider/model:thinking`` — mirroring pi's CLI shorthand.
_REF_RE = re.compile(r"^(?P<body>.+?)(?::(?P<level>off|minimal|low|medium|high|xhigh|max))?$")

THINKING_LEVELS = ("off", "minimal", "low", "medium", "high", "xhigh", "max")


@dataclass(slots=True)
class Resolved:
    model: ModelInfo
    thinking_level: str | None = None

    @property
    def ref(self) -> str:
        return self.model.ref


class ProviderRegistry:
    def __init__(self, catalog: Catalog):
        self.catalog = catalog

    # ------------------------------------------------------------------ resolution

    def resolve(self, ref: str) -> Resolved:
        """Resolve ``provider/model[:level]``.

        The thinking suffix is only treated as such when it is a known level, so model
        ids containing ``:`` (rare but legal) still resolve.
        """
        body, level = _split_thinking(ref)
        return Resolved(model=self.catalog.resolve(body), thinking_level=level)

    def model(self, ref: str) -> ModelInfo:
        return self.resolve(ref).model

    # ------------------------------------------------------------------ capability

    def supports(self, model: ModelInfo) -> bool:
        return model.api in TRANSPORTS

    def require_supported(self, model: ModelInfo) -> ModelInfo:
        if not self.supports(model):
            raise UnsupportedProtocolError(model.api, provider=model.provider, model=model.id)
        return model

    def usable(self, model: ModelInfo) -> bool:
        return self.supports(model) and bool(model.base_url)

    # ------------------------------------------------------------------ search

    def search(self, pattern: str) -> list[ModelInfo]:
        return [m for m in self.catalog.search(pattern) if self.usable(m)]

    def by_provider(self, provider: str) -> list[ModelInfo]:
        prov = self.catalog.provider(provider)
        return sorted(prov.models.values(), key=lambda m: m.id)

    def protocols(self) -> dict[str, int]:
        counts: dict[str, int] = {}
        for provider in self.catalog.providers.values():
            for model in provider.models.values():
                counts[model.api] = counts.get(model.api, 0) + 1
        return dict(sorted(counts.items()))

    def missing_protocols(self) -> dict[str, int]:
        return {api: count for api, count in self.protocols().items() if api not in TRANSPORTS}


def _split_thinking(ref: str) -> tuple[str, str | None]:
    match = _REF_RE.match(ref)
    if not match:
        return ref, None
    level = match.group("level")
    body = match.group("body")
    if level and body:
        return body, level
    return ref, None


def format_table(rows: Iterable[Iterable[str]], headers: tuple[str, ...]) -> str:
    """Tiny column formatter (no rich dependency in the data layer)."""
    data = [[str(c) for c in row] for row in rows]
    widths = [len(h) for h in headers]
    for row in data:
        for i, cell in enumerate(row):
            if i < len(widths):
                widths[i] = max(widths[i], len(cell))
    lines = ["  ".join(h.ljust(widths[i]) for i, h in enumerate(headers)).rstrip()]
    lines.append("  ".join("-" * w for w in widths))
    for row in data:
        lines.append("  ".join(c.ljust(widths[i]) for i, c in enumerate(row)).rstrip())
    return "\n".join(lines)
