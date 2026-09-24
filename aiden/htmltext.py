"""HTML to readable text, with no dependency.

A fetched page is mostly chrome, and the whole value of this module is *which* part of it the agent
sees. Two lessons came from running it against a real documentation page:

1. **`<article>` and `<main>` are not enough.** Sphinx, and most documentation generators, wrap the
   real content in a plain `<div>` and put the navigation in another one. Preferring named elements
   meant the extraction opened with `Navigation`, `next`, `previous` and a breadcrumb instead of the
   API reference — and since output is capped, the chrome pushed the content out of the window.
2. **Entities appear outside text nodes.** ``convert_charrefs`` decodes character references in data
   but not in attributes or in what a regex reads, so a title came through as
   ``pathlib &#8212; Object-oriented``.

So the page is parsed into a small tree, the best content region is chosen by **link density** — the
heuristic readability implementations converged on — and markdown is produced from that region only.
Link density is the signal because navigation is a list of links and prose is not.

Kept in the standard library on purpose: the job is narrow, and a dependency for it would be one more
thing between a fetch and an answer.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from html import unescape
from html.parser import HTMLParser

#: Elements whose contents never belong in readable text.
SKIP = {
    "script",
    "style",
    "noscript",
    "template",
    "svg",
    "canvas",
    "iframe",
    "form",
    "nav",
    "aside",
    "footer",
    "header",
}

#: Elements that can hold the article. Scored rather than trusted.
CANDIDATES = {"article", "main", "div", "section", "td", "body"}

BLOCK = {
    "p",
    "div",
    "section",
    "article",
    "main",
    "ul",
    "ol",
    "li",
    "table",
    "tr",
    "blockquote",
    "pre",
    "figure",
    "figcaption",
    "dl",
    "dt",
    "dd",
    "hr",
}

_HEADINGS = {"h1": "#", "h2": "##", "h3": "###", "h4": "####", "h5": "#####", "h6": "######"}

_VOID = {"br", "hr", "img", "meta", "link", "input", "source", "col", "area", "base", "wbr"}
_WHITESPACE = re.compile(r"[ \t\r\f\v]+")
_BLANK_LINES = re.compile(r"\n{3,}")

#: A region below this many characters of text is not an article, whatever its link density.
MIN_CONTENT_CHARS = 400

#: Class and id hints. Used only as a tie-breaker, never as the primary signal.
POSITIVE_HINTS = ("content", "article", "main", "body", "post", "entry", "document", "markdown")
NEGATIVE_HINTS = ("nav", "menu", "sidebar", "footer", "header", "comment", "related", "breadcrumb")


@dataclass
class Node:
    tag: str
    attrs: dict[str, str] = field(default_factory=dict)
    children: list[Node | str] = field(default_factory=list)

    def text(self) -> str:
        parts: list[str] = []
        for child in self.children:
            parts.append(child if isinstance(child, str) else child.text())
        return "".join(parts)

    def link_text(self) -> int:
        """Characters of text inside ``<a>``, the signal that separates chrome from prose."""
        total = 0
        for child in self.children:
            if isinstance(child, str):
                continue
            if child.tag == "a":
                total += len(child.text())
            else:
                total += child.link_text()
        return total

    def hints(self) -> str:
        return f"{self.attrs.get('class', '')} {self.attrs.get('id', '')}".lower()


class _Builder(HTMLParser):
    """Parses HTML into a node tree, tolerating the malformed markup real pages contain."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.root = Node("body")
        self._stack: list[Node] = [self.root]
        self._skip_depth = 0

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag in SKIP:
            self._skip_depth += 1
            return
        if self._skip_depth:
            return
        node = Node(tag, {k: (v or "") for k, v in attrs})
        self._stack[-1].children.append(node)
        if tag not in _VOID:
            self._stack.append(node)

    def handle_startendtag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag in SKIP or self._skip_depth:
            return
        self._stack[-1].children.append(Node(tag, {k: (v or "") for k, v in attrs}))

    def handle_endtag(self, tag: str) -> None:
        if tag in SKIP:
            self._skip_depth = max(0, self._skip_depth - 1)
            return
        if self._skip_depth:
            return
        # Close the nearest matching ancestor. Unmatched tags (common in the wild) are ignored
        # rather than allowed to corrupt the tree.
        for index in range(len(self._stack) - 1, 0, -1):
            if self._stack[index].tag == tag:
                del self._stack[index:]
                return

    def handle_data(self, data: str) -> None:
        if self._skip_depth or not data:
            return
        self._stack[-1].children.append(data)


def parse(html: str) -> Node:
    builder = _Builder()
    try:
        builder.feed(html)
        builder.close()
    except Exception:  # noqa: S110 - a malformed page must not fail a fetch
        # A parser failure is a property of the page, not of the tool. The partial tree is the
        # useful artefact, and a warning about our own parser would spend context saying nothing
        # about the document.
        pass
    return builder.root


#: How hard to penalise text that is inside a link. Navigation is nearly all links; prose is
#: nearly none, so this is what separates them. Three-to-one came from observing that a plain
#: "subtract link text" still let a link-heavy container outscore the article it wrapped.
LINK_PENALTY = 3.0

#: Candidates scoring within this fraction of the best are treated as tied, and the *most specific*
#: wins. Without it, a container always beats the article inside it, because it contains the same
#: text plus the chrome — which is how a sidebar ended up in the output.
TIE_FRACTION = 0.9


def content_score(node: Node) -> float:
    """How much this node looks like the article: text, minus what is really navigation."""
    text = _normalized_text(node)
    length = len(text)
    if length == 0:
        return 0.0
    score = max(0.0, length - LINK_PENALTY * node.link_text())
    hints = node.hints()
    if any(hint in hints for hint in NEGATIVE_HINTS):
        score *= 0.3
    if any(hint in hints for hint in POSITIVE_HINTS):
        score *= 1.4
    return score


def best_region(root: Node) -> Node:
    """The node most likely to hold the article.

    Falls back to the root when nothing scores meaningfully, because a wrong-but-large region beats
    an empty result: the output budget caps the cost of extra chrome, while returning nothing loses
    the page entirely.
    """
    candidates: list[tuple[float, int, Node]] = []
    for node in _walk(root):
        if node.tag not in CANDIDATES:
            continue
        text = _normalized_text(node)
        if len(text) < MIN_CONTENT_CHARS:
            continue
        candidates.append((content_score(node), len(text), node))

    if not candidates:
        return root

    best_score = max(score for score, _length, _node in candidates)
    if best_score <= 0:
        return root
    # Among near-ties, prefer the smallest region: it is the article rather than the page.
    tied = [entry for entry in candidates if entry[0] >= best_score * TIE_FRACTION]
    return min(tied, key=lambda entry: entry[1])[2]


def _walk(node: Node) -> list[Node]:
    out = [node]
    for child in node.children:
        if isinstance(child, Node):
            out.extend(_walk(child))
    return out


def _normalized_text(node: Node) -> str:
    return _WHITESPACE.sub(" ", node.text()).strip()


# --------------------------------------------------------------------------- markdown


def _to_markdown(node: Node, *, out: list[str] | None = None) -> str:
    parts: list[str] = out if out is not None else []

    def line_break() -> None:
        if parts and not parts[-1].endswith("\n\n"):
            parts.append("\n" if parts[-1].endswith("\n") else "\n\n")

    def emit_text(value: str, *, pre: bool) -> None:
        if pre:
            parts.append(value)
            return
        text = _WHITESPACE.sub(" ", value)
        if not text.strip():
            if parts and not parts[-1].endswith((" ", "\n")):
                parts.append(" ")
            return
        if (
            parts
            and not parts[-1].endswith((" ", "\n", "[", "`", "- "))
            and not text.startswith((" ", ".", ",", ")", "]", ":", ";"))
        ):
            parts.append(" ")
        parts.append(text)

    def render(current: Node, *, pre: bool = False, depth: int = 0) -> None:
        for child in current.children:
            if isinstance(child, str):
                emit_text(child, pre=pre)
                continue
            tag = child.tag
            if tag in _HEADINGS:
                line_break()
                parts.append(f"{_HEADINGS[tag]} ")
                render(child, pre=pre, depth=depth)
                line_break()
            elif tag == "pre":
                line_break()
                parts.append("```\n")
                render(child, pre=True, depth=depth)
                parts.append("\n```")
                line_break()
            elif tag == "code":
                if pre:
                    render(child, pre=True, depth=depth)
                else:
                    parts.append("`")
                    render(child, pre=False, depth=depth)
                    parts.append("`")
            elif tag == "li":
                line_break()
                parts.append("  " * depth + "- ")
                render(child, pre=pre, depth=depth + 1)
                line_break()
            elif tag in {"ul", "ol"}:
                line_break()
                render(child, pre=pre, depth=depth)
                line_break()
            elif tag == "a":
                href = child.attrs.get("href", "")
                label = _normalized_text(child)
                if not label:
                    continue
                parts.append(f"[{label}]({href})" if href else label)
            elif tag == "img":
                src = child.attrs.get("src", "")
                if src:
                    parts.append(f"![{child.attrs.get('alt', '')}]({src})")
            elif tag == "br":
                parts.append("\n")
            elif tag in BLOCK:
                line_break()
                render(child, pre=pre, depth=depth)
                line_break()
            else:
                render(child, pre=pre, depth=depth)

    render(node)
    text = "".join(parts)
    text = re.sub(r"[ \t]+\n", "\n", text)  # trailing spaces only: leading ones are code
    return _BLANK_LINES.sub("\n\n", text).strip()


def html_to_text(html: str) -> str:
    """Extract the readable part of ``html`` as markdown-ish text."""
    return _to_markdown(best_region(parse(html)))


def title_of(html: str) -> str:
    """The document title, with entities decoded.

    ``convert_charrefs`` does not reach a regex, so ``&#8212;`` in a ``<title>`` survived until this
    was fixed — the header of every fetch showed raw entity codes.
    """
    match = re.search(r"<title[^>]*>(.*?)</title>", html, re.IGNORECASE | re.DOTALL)
    if not match:
        return ""
    return _WHITESPACE.sub(" ", unescape(match.group(1))).strip()[:200]
