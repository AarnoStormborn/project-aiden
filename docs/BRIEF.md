# Research Brief — Project Aiden

> Shared context for every research agent working on this repo. Read this before writing.

## What we are building

**Project Aiden** — a personal, *learning-oriented* agentic harness for **coding + research**.
The agent inside it is called **Aiden**.

Not trying to beat Claude Code / Pi / OpenClaw / Aider. The point is to *understand the
mechanisms deeply enough to rebuild them*, and to try things the mainstream harnesses do not:
**continual learning** (the harness gets better the more you use it) and **self-update**
(the agent proposes and applies patches to its own tooling/prompts/skills under guardrails).

Explicit first deliverable after research: **a unique, aesthetically pleasing, informative TUI**
for the agent. A desktop app comes in a later iteration.

## Constraints

- Language/tooling: Python 3.12+, `uv`, repo currently bare (`main.py` is a stub).
- Orchestration uses the **pi** coding agent only (no claude/codex/cursor CLI as the agent).
  Other harnesses are *subjects of study*, not tools we depend on.
- Every claim in a research doc needs a **source link**. Prefer primary sources
  (repos, papers, official docs, author talks/blog posts) over aggregator blogs.
- Date awareness: it is **2026**. Look for the *latest* published methods, not 2023 folklore.

## Output contract for each research agent

Write **exactly one** markdown file at the path given in your prompt. Rules:

1. Use the H1 = the doc title. No frontmatter.
2. Structure:
   - `## TL;DR` — 5–10 bullets, the decisions a reader can make from this doc alone.
   - Body sections — see the *required sections* in your prompt.
   - `## Implications for Aiden` — **mandatory**. Concrete: what we copy, what we skip, why.
   - `## Open questions` — things we could not resolve.
   - `## Sources` — numbered list, `[n] Title — URL`, cited in-body as `[1]`, `[2]`.
3. **250–600 lines.** Dense, no filler, no marketing voice. Tables where comparing things.
4. Include at least one **ASCII or Mermaid diagram** if the topic has structure (pipeline, loop,
   layout, data flow).
5. Include **code snippets** where they teach mechanism (real API shapes, not pseudo-code you
   invented). If you quote a repo, quote the file path.
6. Write the file with the `write` tool in ONE pass if possible; if you must build it up,
   finish the whole document — a half-written file is a failure.
7. Do **not** edit any other file in the repo. Do not create extra files. Your deliverable is
   one file. Do not run `git commit`.
8. When done, reply with: `DONE <path> <line count> <one-line summary>`.

## Do not collide

Each agent owns one file. Other agents are writing sibling files concurrently. Never touch
`docs/README.md`, `docs/architecture/`, or another agent's file.
