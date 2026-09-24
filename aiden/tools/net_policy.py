"""Network policy: what a fetch may reach, decided before the request is made.

A URL is opaque, so nothing about it is *provable* in the way a command prefix is. Two things
follow:

1. **Every fetch asks**, unless its host matches an allowlist. Prompt fatigue is the failure mode
   here — a user who is asked about every documentation lookup learns to click yes, so the allowlist
   is what keeps the gate meaningful rather than decorative.
2. **The hostname is not the target.** Checking the string alone is defeated by a name that resolves
   to ``127.0.0.1``, so the check resolves first and inspects the address it actually got.

The address rules exist because this process can reach things the user cannot see: a cloud metadata
endpoint hands out credentials, and a port on localhost may be an unauthenticated admin interface.
"""

from __future__ import annotations

import ipaddress
import os
import socket
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from urllib.parse import urlsplit

ALLOWED_SCHEMES = ("http", "https")

#: Hosts that are never fetchable, whatever DNS says.
NEVER_HOSTS = ("localhost", "localhost.localdomain", "metadata.google.internal")

#: The address a fetch must never reach, named explicitly so the reason can too.
METADATA_ADDRESSES = ("169.254.169.254", "fd00:ec2::254")


@dataclass(frozen=True, slots=True)
class FetchDecision:
    allowed: bool
    reason: str = ""
    #: True when the host is on the user's allowlist, which is what skips approval.
    allowlisted: bool = False


@dataclass
class FetchPolicy:
    """Rules for what may be fetched."""

    allowlist: tuple[str, ...] = field(default_factory=tuple)
    #: Tests inject a resolver so SSRF cases can be exercised without real DNS; production uses the
    #: system resolver.
    resolver: Callable[[str], list[str]] | None = None

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None) -> FetchPolicy:
        resolved: Mapping[str, str] = os.environ if env is None else env
        raw = resolved.get("AIDEN_FETCH_ALLOW") or ""
        hosts = tuple(h.strip().lower() for h in raw.split(",") if h.strip())
        return cls(allowlist=hosts)

    # ------------------------------------------------------------------ decision

    def check(self, url: str) -> FetchDecision:
        """Whether ``url`` may be fetched, with a reason a human can act on."""
        parts = urlsplit(url.strip())
        if parts.scheme.lower() not in ALLOWED_SCHEMES:
            return FetchDecision(
                False,
                f"refused: {parts.scheme or 'no'} scheme. Only http and https can be fetched — "
                "file:, ftp: and data: reach things that are not the web.",
            )
        host = (parts.hostname or "").lower()
        if not host:
            return FetchDecision(False, "refused: the URL has no host")

        if host in NEVER_HOSTS:
            return FetchDecision(
                False,
                f"refused: '{host}' is this machine. Fetching it would read whatever the user has "
                "running locally, which is not what a web lookup should do.",
            )

        addresses = self._addresses(host)
        for address in addresses:
            blocked = _blocked_address_reason(address)
            if blocked:
                return FetchDecision(False, f"refused: '{host}' resolves to {address}, {blocked}")

        allowlisted = any(host == entry or host.endswith(f".{entry}") for entry in self.allowlist)
        return FetchDecision(True, allowlisted=allowlisted)

    def _addresses(self, host: str) -> list[str]:
        """Every address ``host`` resolves to, or ``[]`` when it cannot be resolved.

        A test can inject a resolver, which is the only way to exercise the SSRF cases: a name that
        resolves to loopback cannot be produced by real DNS on demand.
        """
        try:
            return [str(ipaddress.ip_address(host))]
        except ValueError:
            pass  # not a literal address, so resolve it

        if self.resolver is not None:
            try:
                return [str(address) for address in self.resolver(host)]
            except Exception:
                return []
        try:
            infos = socket.getaddrinfo(host, None)
        except OSError:
            # Unresolvable is not "unsafe"; let the fetch fail with a network error it can explain.
            return []
        return [str(info[4][0]) for info in infos]


def _blocked_address_reason(address: str) -> str:
    """Why ``address`` must not be fetched, or ``""`` when it is fine."""
    if address in METADATA_ADDRESSES:
        return "which is the cloud instance metadata endpoint (it hands out credentials)"
    try:
        ip = ipaddress.ip_address(address)
    except ValueError:
        return ""
    if ip.is_loopback:
        return "which is this machine"
    if ip.is_link_local:
        return "which is a link-local address (metadata and other host services live there)"
    if ip.is_private:
        return "which is on a private network this machine can reach"
    if ip.is_reserved or ip.is_multicast or ip.is_unspecified:
        return "which is a reserved address"
    return ""
