"""Provider quirks that live outside the catalog's ``compat`` block.

Catalog ``compat`` flags are *model-level* translation switches (which field name, which
role). Some requirements are *provider-level and transport-independent* — they must be
attached to every request regardless of protocol. Those live here.

Discovery: opencode-go rejects requests without ``x-opencode-session`` ("cannot be routed
efficiently"), and pi injects it for the whole ``opencode``/``opencode-go`` provider via
``providers/opencode-headers.ts``.
"""

from __future__ import annotations

import uuid

OPENCODE_SESSION_HEADER = "x-opencode-session"

# Providers that require a per-conversation routing header.
_SESSION_HEADER_PROVIDERS = {
    "opencode": OPENCODE_SESSION_HEADER,
    "opencode-go": OPENCODE_SESSION_HEADER,
}


def requires_session_id(provider: str) -> bool:
    return provider in _SESSION_HEADER_PROVIDERS


def new_session_id() -> str:
    return uuid.uuid4().hex


def provider_headers(provider: str, session_id: str | None) -> dict[str, str]:
    """Headers every request to ``provider`` must carry."""
    header = _SESSION_HEADER_PROVIDERS.get(provider)
    if header and session_id:
        return {header: session_id}
    return {}
