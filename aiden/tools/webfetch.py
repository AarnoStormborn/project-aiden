"""``web_fetch`` — read a URL as text an agent can reason about.

The seventh tool. What it returns is *extracted text*, not bytes: a page's navigation and scripts are
not information, and `research/02` §2's whole point is that tool output is the scarce resource.

Bounds, each for a specific failure:

- **Size.** 2 MB is the reading limit — a 500 MB response is a denial of service, not a document. The
  stop is reported, not silent.
- **Time.** 15 s. A hanging fetch would stall the run with no way to tell why.
- **Output.** 16 KB with a spill path past 4x, the same discipline as every other tool.
- **Content type.** Only text-ish responses. `application/octet-stream` is refused with the type
  named, because returning binary noise as text costs context and teaches nothing.

A session-level cache exists because re-fetching a page the run already read is pure waste, and a
fetch is the most expensive thing a tool can do.

The second-model compressor from `architecture` §9 ("`web_fetch` gets a small model to compress") is
deliberately not here yet: it needs async tools plus nested cost attribution. The budget makes the
raw case acceptable first; `docs/plan/v0.2c-webfetch.md` records the trigger.
"""

from __future__ import annotations

import time
from typing import Any

from .. import config
from ..htmltext import html_to_text, title_of
from ..providers.types import ToolSpec
from .net_policy import FetchPolicy
from .output import cap_text, spill
from .types import ToolContext, ToolResult

TIMEOUT_S = 15.0
MAX_RESPONSE_BYTES = 2 * 1024 * 1024

#: Content types worth turning into text. Anything else is refused with the type named.
TEXT_TYPES = (
    "text/",
    "application/json",
    "application/xml",
    "application/xhtml",
    "application/javascript",
    "application/x-yaml",
)

USER_AGENT = "aiden/0.1 (+https://github.com/AarnoStormborn/project-aiden)"


class WebFetchTool:
    name = "web_fetch"
    mutating = False  # it changes nothing locally, but it does reach outward

    def spec(self) -> ToolSpec:
        return ToolSpec(
            name="web_fetch",
            description=(
                "Fetch a URL and return its readable text, with headings, lists, code blocks and "
                "link targets preserved. HTML is extracted, not returned raw. Content is capped, so "
                "long pages are elided with a spill path. Fetches need the user's approval unless "
                "the host is allowlisted, and requests to this machine or a private network are "
                "refused."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "url": {"type": "string", "description": "The http(s) URL to fetch."},
                    "reason": {
                        "type": "string",
                        "description": "Why this page is needed, shown in the approval prompt.",
                    },
                },
                "required": ["url"],
            },
        )

    # ------------------------------------------------------------------ gate

    def approval_required(self, args: dict[str, Any], ctx: ToolContext) -> bool:
        """Allowlisted hosts skip approval; everything else asks.

        Refused URLs return ``True`` here so the gate asks — but the policy refusal is checked first
        in :meth:`run`, so a refused URL never reaches a prompt at all.
        """
        decision = self._policy(ctx).check(str(args.get("url", "")))
        if not decision.allowed:
            return True  # will be refused by run(); asking about it would waste the user's time
        return not decision.allowlisted

    def preview(self, args: dict[str, Any], ctx: ToolContext) -> str | None:
        url = str(args.get("url", "")).strip()
        if not url:
            return None
        decision = self._policy(ctx).check(url)
        if decision.allowed and decision.allowlisted:
            return None
        reasons = [f"+ $ fetch {url}"]
        if args.get("reason"):
            reasons.insert(0, f"# {args['reason']}")
        reasons.append("")
        if not decision.allowed:
            reasons.append(f"# refused: {decision.reason}")
        else:
            reasons.append(
                "# requires approval: a URL is opaque, and a request leaves this machine"
            )
            reasons.append("# (a query string can carry data out; check the URL above)")
        return "\n".join(reasons)

    # ------------------------------------------------------------------ run

    def run(self, args: dict[str, Any], ctx: ToolContext) -> ToolResult:
        url = str(args.get("url", "")).strip()
        if not url:
            return ToolResult.error("url is required")

        decision = self._policy(ctx).check(url)
        if not decision.allowed:
            return ToolResult.error(decision.reason)

        cached = ctx.fetch_cache.get(url)
        if cached is not None:
            return ToolResult(
                output=f"{cached}\n\n[from this session's cache; no request was made]",
                meta={"url": url, "cached": True},
            )

        return self._fetch(url, ctx)

    def _fetch(self, url: str, ctx: ToolContext) -> ToolResult:
        import httpx

        client = ctx.http_client or httpx.Client(
            timeout=TIMEOUT_S, follow_redirects=True, headers={"user-agent": USER_AGENT}
        )
        started = time.perf_counter()
        try:
            with client.stream("GET", url) as response:
                if response.status_code >= 400:
                    return ToolResult.error(
                        f"fetch failed: HTTP {response.status_code} for {url}. "
                        "The page may have moved or need different headers."
                    )
                content_type = response.headers.get("content-type", "")
                if not _is_text(content_type):
                    return ToolResult.error(
                        f"refused: {url} returned {content_type or 'an unknown type'}, which is not "
                        "readable text."
                    )
                body, truncated_at_limit = _read_bounded(response)
        except httpx.TimeoutException:
            return ToolResult.error(f"fetch timed out after {TIMEOUT_S:.0f}s: {url}")
        except httpx.HTTPError as exc:
            return ToolResult.error(f"fetch failed: {type(exc).__name__}: {exc}")
        duration_ms = int((time.perf_counter() - started) * 1000)

        text = body
        title = ""
        if "html" in content_type.lower() or body.lstrip().lower().startswith(
            ("<!doctype", "<html")
        ):
            title = title_of(body)
            text = html_to_text(body)

        notes: list[str] = []
        if truncated_at_limit:
            notes.append(
                f"stopped reading at {MAX_RESPONSE_BYTES // (1024 * 1024)} MB; the page is larger"
            )
        capped = cap_text(text, max_bytes=config.READ_MAX_BYTES, max_lines=2_000)
        if capped.truncated and len(text.encode()) > config.READ_MAX_BYTES * 4:
            path = spill(text, name="webfetch", directory=ctx.spill_dir)
            notes.append(f"the full extraction is at {path}")
            capped.hint = f"{capped.hint} [{notes[-1]}]" if capped.hint else f"[{notes[-1]}]"
        elif capped.truncated:
            notes.append("output elided to fit the budget")

        header = f"{url}"
        if title:
            header = f"{title}\n{url}"
        summary = f" · {duration_ms}ms"
        if notes:
            summary += " · " + "; ".join(notes)

        payload = f"{header}\n{summary}\n\n{capped.text}"
        ctx.fetch_cache[url] = payload
        return ToolResult(
            output=payload,
            truncated=capped.truncated,
            hint=capped.hint,
            meta={
                "url": url,
                "status": 200,
                "bytes": len(body),
                "duration_ms": duration_ms,
                "truncated_at_limit": truncated_at_limit,
                "cached": False,
            },
        )

    def _policy(self, ctx: ToolContext) -> FetchPolicy:
        return ctx.fetch_policy or FetchPolicy.from_env()


def _is_text(content_type: str) -> bool:
    lowered = content_type.lower()
    return any(lowered.startswith(kind) or kind in lowered for kind in TEXT_TYPES)


def _read_bounded(response: Any) -> tuple[str, bool]:
    """Read at most ``MAX_RESPONSE_BYTES``, reporting whether the limit was hit."""
    chunks: list[bytes] = []
    total = 0
    for chunk in response.iter_bytes():
        chunks.append(chunk)
        total += len(chunk)
        if total >= MAX_RESPONSE_BYTES:
            return b"".join(chunks).decode("utf-8", "replace"), True
    return b"".join(chunks).decode("utf-8", "replace"), False
