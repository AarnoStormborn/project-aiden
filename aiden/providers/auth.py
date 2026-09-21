"""Credential resolution.

Order (see docs/plan/providers.md §5):
1. explicit ``api_key`` argument (tests, CLI ``--api-key``)
2. provider env var (``ANTHROPIC_API_KEY``, ``OPENCODE_API_KEY``, ``DEEPSEEK_API_KEY``, …)
3. ``~/.aiden/auth.json``
4. nothing → :class:`AuthError` at request time

``aiden providers import pi`` copies pi's ``auth.json`` in, so migration is one command and
we never write to pi's file.
"""

from __future__ import annotations

import json
import os
import time
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

from .errors import AuthError

AIDEN_HOME = Path(os.environ.get("AIDEN_HOME", Path.home() / ".aiden"))
PI_AUTH_FILE = Path.home() / ".pi" / "agent" / "auth.json"

# Mirrors pi's env map (packages/ai/src/env-api-keys.ts). Providers absent here fall back
# to PROVIDER_API_KEY derived from the provider id.
ENV_KEYS: dict[str, tuple[str, ...]] = {
    "anthropic": ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN", "ANTHROPIC_OAUTH_TOKEN"),
    "openai": ("OPENAI_API_KEY",),
    "deepseek": ("DEEPSEEK_API_KEY",),
    "opencode": ("OPENCODE_API_KEY",),
    "opencode-go": ("OPENCODE_API_KEY",),
    "commandcode": ("COMMANDCODE_API_KEY",),
    "google": ("GEMINI_API_KEY", "GOOGLE_API_KEY"),
    "openrouter": ("OPENROUTER_API_KEY",),
    "xai": ("XAI_API_KEY",),
    "groq": ("GROQ_API_KEY",),
    "mistral": ("MISTRAL_API_KEY",),
    "moonshotai": ("MOONSHOT_API_KEY",),
    "moonshotai-cn": ("MOONSHOT_API_KEY",),
    "together": ("TOGETHER_API_KEY",),
    "fireworks": ("FIREWORKS_API_KEY",),
    "cerebras": ("CEREBRAS_API_KEY",),
    "nvidia": ("NVIDIA_API_KEY",),
    "huggingface": ("HF_TOKEN",),
    "meta": ("META_API_KEY",),
    "minimax": ("MINIMAX_API_KEY",),
    "zai": ("ZAI_API_KEY",),
    "kimi-coding": ("KIMI_API_KEY",),
    "github-copilot": ("COPILOT_GITHUB_TOKEN",),
    "xiaomi": ("XIAOMI_API_KEY",),
}

CredentialType = Literal["api_key", "oauth"]


@dataclass(slots=True)
class Credential:
    provider: str
    type: CredentialType
    key: str
    source: str = ""
    expires: int | None = None

    @property
    def expired(self) -> bool:
        return bool(self.expires and self.expires / 1000 < time.time())

    @property
    def display(self) -> str:
        if len(self.key) <= 8:
            return "<redacted>"
        return f"{self.key[:4]}…{self.key[-4:]}"


def auth_file_path(explicit: Path | None = None) -> Path:
    if explicit:
        return explicit
    env = os.environ.get("AIDEN_AUTH_FILE")
    return Path(env) if env else AIDEN_HOME / "auth.json"


def env_var_names(provider: str) -> tuple[str, ...]:
    if provider in ENV_KEYS:
        return ENV_KEYS[provider]
    derived = "".join(ch if ch.isalnum() else "_" for ch in provider).upper()
    return (f"{derived}_API_KEY",)


def load_auth_file(path: Path | None = None) -> dict[str, Any]:
    resolved = auth_file_path(path)
    if not resolved.is_file():
        return {}
    try:
        data = json.loads(resolved.read_text())
    except Exception:
        return {}
    return data if isinstance(data, dict) else {}


def save_auth_file(entries: dict[str, Any], path: Path | None = None) -> Path:
    resolved = auth_file_path(path)
    resolved.parent.mkdir(parents=True, exist_ok=True)
    resolved.write_text(json.dumps(entries, indent=2, sort_keys=True))
    resolved.chmod(0o600)
    return resolved


def resolve(
    provider: str,
    *,
    api_key: str | None = None,
    env: Mapping[str, str] | None = None,
    path: Path | None = None,
) -> Credential | None:
    """Best available credential for ``provider``, or ``None`` if unauthenticated."""
    # os.environ is an os._Environ, not a dict[str, str]; Mapping is the honest type.
    resolved_env: Mapping[str, str] = os.environ if env is None else env

    if api_key:
        return Credential(provider=provider, type="api_key", key=api_key, source="argument")

    for name in env_var_names(provider):
        value = resolved_env.get(name)
        if value:
            return Credential(provider=provider, type="api_key", key=value, source=f"env:{name}")

    entry = load_auth_file(path).get(provider)
    if isinstance(entry, dict):
        kind = entry.get("type")
        if kind == "api_key" and entry.get("key"):
            return Credential(
                provider=provider, type="api_key", key=str(entry["key"]), source="auth_file"
            )
        if kind == "oauth" and entry.get("access"):
            # Command Code stores a non-expiring API key in oauth shape; treat access as
            # the bearer and surface expiry rather than silently refreshing.
            return Credential(
                provider=provider,
                type="oauth",
                key=str(entry["access"]),
                source="auth_file",
                expires=int(entry["expires"]) if entry.get("expires") else None,
            )
        if entry.get("key"):
            return Credential(
                provider=provider, type="api_key", key=str(entry["key"]), source="auth_file"
            )

    return None


def require(provider: str, **kw) -> Credential:
    cred = resolve(provider, **kw)
    if cred is None:
        names = " or ".join(env_var_names(provider))
        raise AuthError(
            f"no credentials for '{provider}'. set {names} or add it to {auth_file_path()}",
            provider=provider,
        )
    return cred


def status(providers: list[str], path: Path | None = None) -> list[dict[str, Any]]:
    """Auth status rows for the CLI (no secrets)."""
    rows = []
    for provider in providers:
        cred = resolve(provider, path=path)
        rows.append(
            {
                "provider": provider,
                "authenticated": cred is not None,
                "type": cred.type if cred else "",
                "source": cred.source if cred else "",
                "key": cred.display if cred else "",
                "expired": bool(cred and cred.expired),
            }
        )
    return rows


def import_pi(
    *,
    pi_path: Path | None = None,
    target: Path | None = None,
    providers: list[str] | None = None,
    overwrite: bool = False,
) -> list[str]:
    """Copy credentials out of pi's auth.json. Returns the provider ids written."""
    source = pi_path or PI_AUTH_FILE
    if not source.is_file():
        raise FileNotFoundError(f"pi auth file not found: {source}")

    raw = json.loads(source.read_text())
    existing = load_auth_file(target)
    written: list[str] = []

    for provider, entry in raw.items():
        if providers and provider not in providers:
            continue
        if provider in existing and not overwrite:
            continue
        if not isinstance(entry, dict):
            continue
        if entry.get("type") == "api_key" and entry.get("key"):
            existing[provider] = {"type": "api_key", "key": entry["key"]}
        elif entry.get("type") == "oauth" and entry.get("access"):
            existing[provider] = {
                "type": "oauth",
                "access": entry["access"],
                "refresh": entry.get("refresh", ""),
                "expires": entry.get("expires", 0),
            }
        else:
            continue
        written.append(provider)

    if written:
        save_auth_file(existing, target)
    return written
