"""web_fetch: the policy, the extractor, and the bounds.

No test here touches the network. HTTP is exercised through ``httpx.MockTransport``, which means the
suite is fast, offline, and can produce responses no real server would give us on demand — a 5 MB
page, a binary content type, a timeout.
"""

from __future__ import annotations

import httpx
import pytest

from aiden import config
from aiden.htmltext import html_to_text, title_of
from aiden.tools import TOOLS, ToolContext, execute
from aiden.tools.net_policy import FetchPolicy
from aiden.tools.types import ReadState, preview_of

# --------------------------------------------------------------------------- policy


@pytest.fixture
def policy() -> FetchPolicy:
    return FetchPolicy(allowlist=("docs.python.org",))


def test_https_is_allowed(policy: FetchPolicy):
    assert policy.check("https://example.com/a").allowed


@pytest.mark.parametrize(
    "url", ["file:///etc/passwd", "ftp://host/x", "data:text/plain,hi", "gopher://x"]
)
def test_non_http_schemes_are_refused(url: str, policy: FetchPolicy):
    decision = policy.check(url)
    assert not decision.allowed
    assert "scheme" in decision.reason


@pytest.mark.parametrize(
    "url",
    [
        "http://localhost:8080/admin",
        "http://127.0.0.1:9200/",
        "http://[::1]:8000/",
        "http://169.254.169.254/latest/meta-data/",
        "http://10.0.0.5/",
        "http://192.168.1.1/",
        "http://172.16.4.4/",
    ],
)
def test_this_machine_and_private_networks_are_refused(url: str, policy: FetchPolicy):
    """A fetch must not reach the host it runs on, or the network it can see."""
    decision = policy.check(url)
    assert not decision.allowed, url


def test_a_public_address_is_allowed(policy: FetchPolicy):
    assert policy.check("https://93.184.216.34/").allowed


def test_a_name_that_resolves_to_loopback_is_refused():
    """Checking the hostname string alone is defeated by DNS; the resolved address is what counts."""
    policy = FetchPolicy(resolver=lambda _host: ["127.0.0.1"])
    decision = policy.check("https://totally-normal.example.com/")
    assert not decision.allowed
    assert "resolves to" in decision.reason


def test_a_name_that_resolves_to_the_metadata_endpoint_is_refused():
    policy = FetchPolicy(resolver=lambda _host: ["169.254.169.254"])
    assert not policy.check("https://metadata.example.com/").allowed


def test_an_unresolvable_host_is_not_a_policy_failure():
    """Let the fetch fail with a network error it can explain, rather than pretending to decide."""
    policy = FetchPolicy(resolver=lambda _host: [])
    assert policy.check("https://nonexistent.invalid/").allowed


def test_the_allowlist_marks_the_decision(policy: FetchPolicy):
    assert policy.check("https://docs.python.org/3/library/os.html").allowlisted
    assert not policy.check("https://example.com/").allowlisted


def test_a_subdomain_of_an_allowlisted_host_counts(policy: FetchPolicy):
    assert policy.check("https://bugs.docs.python.org/x").allowlisted


def test_a_lookalike_host_is_not_allowlisted(policy: FetchPolicy):
    """`notdocs.python.org.attacker.com` must not match a `docs.python.org` rule."""
    assert not policy.check("https://docs.python.org.attacker.com/").allowlisted
    assert not policy.check("https://evil-docs.python.org/").allowlisted


def test_a_url_without_a_host_is_refused():
    assert not FetchPolicy().check("http://").allowed


def test_the_allowlist_comes_from_the_environment(monkeypatch):
    monkeypatch.setenv("AIDEN_FETCH_ALLOW", "docs.python.org, github.com")
    policy = FetchPolicy.from_env()
    assert policy.allowlist == ("docs.python.org", "github.com")


def test_a_query_string_is_allowed_but_still_needs_approval():
    """A URL can carry data out; the gate cannot judge that, so it asks and shows the whole URL."""
    policy = FetchPolicy()
    decision = policy.check("https://example.com/?d=secrets")
    assert decision.allowed
    assert not decision.allowlisted


# --------------------------------------------------------------------------- extraction


def test_scripts_and_styles_are_dropped():
    text = html_to_text(
        "<html><head><style>b{color:red}</style></head>"
        "<body><script>var x = 1;</script><p>Real prose.</p></body></html>"
    )
    assert "Real prose." in text
    assert "color:red" not in text
    assert "var x" not in text


def test_headings_become_markdown():
    text = html_to_text("<h1>Title</h1><h2>Sub</h2><p>Body</p>")
    assert "# Title" in text
    assert "## Sub" in text


def test_links_keep_their_target():
    """A link's href is often the most useful thing on the page."""
    text = html_to_text('<p>See <a href="https://example.com/x">the docs</a>.</p>')
    assert "[the docs](https://example.com/x)" in text


def test_lists_become_bullets():
    text = html_to_text("<ul><li>one</li><li>two</li></ul>")
    assert "- one" in text
    assert "- two" in text


def test_code_blocks_are_preserved_verbatim():
    text = html_to_text("<pre><code>def f():\n    return 1</code></pre>")
    assert "def f():" in text
    assert "    return 1" in text, "indentation inside a code block is content"
    assert "```" in text


def test_navigation_is_dropped():
    text = html_to_text("<nav><a href='/'>Home</a></nav><main><p>Content</p></main>")
    assert "Content" in text
    assert "Home" not in text


def test_an_article_region_is_preferred_when_substantial():
    article = "<p>" + ("Substantial prose. " * 40) + "</p>"
    text = html_to_text(f"<div>Boilerplate sidebar</div><article>{article}</article>")
    assert "Substantial prose." in text
    assert "Boilerplate sidebar" not in text


def test_a_stub_article_does_not_hide_the_page():
    """A teaser <article> is worse than the whole document."""
    text = html_to_text("<article>Short teaser.</article><div>The real body text.</div>")
    assert "The real body text." in text


def test_malformed_html_still_yields_what_it_can():
    text = html_to_text("<p>unclosed <b>bold <div><span>nested")
    assert "unclosed" in text


def test_whitespace_is_collapsed():
    text = html_to_text("<p>many      spaces</p><p>and\n\nnewlines</p>")
    assert "many spaces" in text
    assert "\n\n\n" not in text


def test_the_title_is_extracted():
    assert title_of("<html><head><title> My Page </title></head></html>") == "My Page"
    assert title_of("<html></html>") == ""


# --------------------------------------------------------------------------- the tool


@pytest.fixture
def ctx(tmp_path, monkeypatch) -> ToolContext:
    monkeypatch.setattr(config, "SPILL_DIR", tmp_path / "spill")
    project = tmp_path / "proj"
    project.mkdir()
    return ToolContext(
        cwd=project,
        read_state=ReadState(),
        spill_dir=tmp_path / "spill",
        fetch_policy=FetchPolicy(),
    )


def with_transport(ctx: ToolContext, handler) -> ToolContext:
    ctx.http_client = httpx.Client(transport=httpx.MockTransport(handler))
    return ctx


PAGE = (
    "<html><head><title>Docs</title></head><body>"
    "<nav>Navigation</nav><article><h1>Heading</h1><p>Body text.</p>"
    "<pre><code>code()</code></pre></article></body></html>"
)


def test_a_page_is_fetched_and_extracted(ctx: ToolContext):
    with_transport(
        ctx, lambda request: httpx.Response(200, headers={"content-type": "text/html"}, text=PAGE)
    )
    result = execute("web_fetch", {"url": "https://example.com/docs"}, ctx)

    assert not result.is_error, result.output
    assert "Docs" in result.output, "the title belongs in the header"
    assert "Heading" in result.output
    assert "Body text." in result.output
    assert "<h1>" not in result.output, "raw HTML must not reach the model"
    assert "Navigation" not in result.output


def test_plain_text_is_passed_through(ctx: ToolContext):
    with_transport(
        ctx,
        lambda request: httpx.Response(
            200, headers={"content-type": "text/plain"}, text="raw text"
        ),
    )
    result = execute("web_fetch", {"url": "https://example.com/x.txt"}, ctx)
    assert "raw text" in result.output


def test_a_binary_response_is_refused_with_its_type(ctx: ToolContext):
    with_transport(
        ctx,
        lambda request: httpx.Response(
            200, headers={"content-type": "application/octet-stream"}, content=b"\x00\x01"
        ),
    )
    result = execute("web_fetch", {"url": "https://example.com/blob"}, ctx)
    assert result.is_error
    assert "application/octet-stream" in result.output
    assert "not readable text" in result.output


def test_an_http_error_is_reported_with_its_status(ctx: ToolContext):
    with_transport(ctx, lambda request: httpx.Response(404, text="nope"))
    result = execute("web_fetch", {"url": "https://example.com/gone"}, ctx)
    assert result.is_error
    assert "404" in result.output


def test_a_timeout_is_reported(ctx: ToolContext):
    def handler(request):
        raise httpx.ReadTimeout("too slow", request=request)

    with_transport(ctx, handler)
    result = execute("web_fetch", {"url": "https://example.com/slow"}, ctx)
    assert result.is_error
    assert "timed out" in result.output


def test_output_is_capped_with_a_spill_path(ctx: ToolContext):
    huge = "<p>" + ("word " * 200_000) + "</p>"
    with_transport(
        ctx, lambda request: httpx.Response(200, headers={"content-type": "text/html"}, text=huge)
    )
    result = execute("web_fetch", {"url": "https://example.com/huge"}, ctx)

    assert result.truncated
    assert len(result.output.encode()) <= config.READ_MAX_BYTES * 3
    assert "spill" in result.hint or "the full extraction is at" in result.hint


def test_a_response_larger_than_the_reading_limit_says_so(ctx: ToolContext):
    oversized = "x" * (3 * 1024 * 1024)
    with_transport(
        ctx,
        lambda request: httpx.Response(200, headers={"content-type": "text/plain"}, text=oversized),
    )
    result = execute("web_fetch", {"url": "https://example.com/big"}, ctx)
    assert result.meta["truncated_at_limit"] is True
    assert "stopped reading at 2 MB" in result.output


def test_the_same_url_is_fetched_once(ctx: ToolContext):
    calls: list[str] = []

    def handler(request):
        calls.append(str(request.url))
        return httpx.Response(200, headers={"content-type": "text/plain"}, text="body")

    with_transport(ctx, handler)
    execute("web_fetch", {"url": "https://example.com/a"}, ctx)
    second = execute("web_fetch", {"url": "https://example.com/a"}, ctx)

    assert len(calls) == 1, "the session cache must prevent a second request"
    assert "cache" in second.output.lower()
    assert second.meta["cached"] is True


def test_a_refused_url_never_makes_a_request(ctx: ToolContext):
    """The policy check must come before the network, not after."""
    calls: list[str] = []
    with_transport(ctx, lambda request: calls.append(str(request.url)) or httpx.Response(200))
    result = execute("web_fetch", {"url": "http://169.254.169.254/latest/meta-data/"}, ctx)

    assert result.is_error
    assert calls == [], "a refused URL must not be requested at all"
    assert "metadata" in result.output


def test_missing_url_is_refused(ctx: ToolContext):
    result = execute("web_fetch", {}, ctx)
    assert result.is_error
    assert "required" in result.output


# --------------------------------------------------------------------------- gate


def test_a_non_allowlisted_host_needs_approval(ctx: ToolContext):
    assert TOOLS["web_fetch"].approval_required({"url": "https://example.com/"}, ctx) is True


def test_an_allowlisted_host_does_not(ctx: ToolContext):
    ctx.fetch_policy = FetchPolicy(allowlist=("docs.python.org",))
    assert TOOLS["web_fetch"].approval_required({"url": "https://docs.python.org/3/"}, ctx) is False


def test_an_allowlisted_host_has_no_preview(ctx: ToolContext):
    ctx.fetch_policy = FetchPolicy(allowlist=("docs.python.org",))
    assert preview_of(TOOLS["web_fetch"], {"url": "https://docs.python.org/3/"}, ctx) is None


def test_the_preview_shows_the_whole_url_and_why(ctx: ToolContext):
    """A query string can carry data out, so the user must see the URL in full."""
    diff = preview_of(
        TOOLS["web_fetch"],
        {"url": "https://example.com/?d=secret", "reason": "check the docs"},
        ctx,
    )
    assert diff is not None
    assert "d=secret" in diff
    assert "opaque" in diff or "query string" in diff


def test_web_fetch_is_not_mutating(ctx: ToolContext):
    """It changes nothing locally, so it is not gated by the write path — the fetch policy governs it."""
    from aiden.tools import MUTATING_TOOLS

    assert TOOLS["web_fetch"].mutating is False
    assert "web_fetch" not in MUTATING_TOOLS
