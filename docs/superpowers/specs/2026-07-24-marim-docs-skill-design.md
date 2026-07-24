# Design: marim-docs skill

**Date:** 2026-07-24
**Status:** Approved
**Scope:** Project-scoped personal skill for reading Marim documentation

## Problem

Marim has ~30 doc files across `docs/` and root-level `.md` files. Finding the right
doc for a question requires knowing the directory structure and what each file covers.
A skill that routes questions to the right doc and reads it on demand would save time
on lookups, overviews, and task-relevant doc discovery.

## Use Cases

1. **Lookup** — "How do sessions work?" → route to `docs/guides/sessions.md`, read it, answer.
2. **Overview** — "Give me an overview of the system" → read key docs, synthesize.
3. **Task-relevant routing** — "I'm adding a new LSP plugin, what docs are relevant?" → find applicable docs, read them.

## Approach: Static index + read at runtime

A curated index (`index.md`) maps each doc to its purpose, keywords, and example
trigger questions. The skill matches the user's question against the index, reads
the relevant doc(s), and answers from the current content. A fallback reads
`docs/README.md` when the index doesn't cover the question.

Chosen over dynamic scanning because docs change infrequently relative to skill
invocations, and the index provides instant routing with minimal context usage.

## Location

```
.marim/skills/marim-docs/
  SKILL.md       ← skill instructions
  index.md       ← curated doc index
```

## Index Format

Three sections — root docs, guides, reference & SDK. Each entry:

```markdown
### `docs/guides/sessions.md`
**Purpose:** Sessions, compaction, checkpoints, /rewind
**Topics:** session store, compaction, checkpoint manager, git snapshots, autoname
**When to ask:** "how do sessions work?", "what is compaction?", "/rewind", "checkpoint"
```

### Covered Files

**Root docs:**
- `README.md` — project overview, features, install, usage, config
- `CLAUDE.md` — project instructions for Claude Code (architecture, conventions, commands)
- `CONTRIBUTING.md` — dev setup, conventions, PR checklist
- `coding-guidelines.md` — code style rules and design principles
- `SECURITY.md` — trust/permission model
- `CHANGELOG.md` — release history
- `ROADMAP.md` — project direction

**Guides (`docs/guides/`):**
- `headless.md` — one-shot / CI mode
- `hooks.md` — lifecycle hooks
- `mcp.md` — MCP servers
- `sessions.md` — sessions, compaction, checkpoints, /rewind
- `skills-and-memory.md` — AGENTS.md, skills, memory, scratchpad
- `subagents.md` — sub-agents and background jobs
- `trust.md` — trust/permission model
- `tui.md` — interactive TUI reference
- `workflows.md` — dynamic workflows

**Reference & SDK (`docs/reference/`, `docs/sdk/`):**
- `reference/configuration.md` — every MARIM_* env var
- `reference/serve-api.md` — marim serve HTTP daemon API
- `sdk/README.md` — SDK overview
- `sdk/builder.md` — HarnessBuilder with_* methods
- `sdk/capabilities.md` — pydantic-ai capabilities marim ships
- `sdk/custom-tools.md` — tool signature, gating, collisions
- `sdk/getting-started.md` — install, quickstart
- `sdk/integrations.md` — MCP, LSP, forge, hooks, bash policy
- `sdk/sessions-and-state.md` — what touches disk
- `sdk/subagents.md` — AgentDef, grants, depth ceiling
- `sdk/testing.md` — network-free turn tests
- `sdk/turns.md` — run_turn, approval, Mode, streaming
- `sdk/tutorial-daily-report.md` — end-to-end walkthrough

**Other docs:**
- `docs/architecture.md` — codebase map and invariants
- `docs/embedding.md` — HarnessBuilder quickstart
- `docs/plugins.md` — authoring plugins
- `docs/lsp-plugins.md` — adding LSP provider plugins

## Skill Workflow

1. Match user's question against index topics and trigger phrases.
2. If match found → read the indexed doc(s) via `read_file`, answer from content.
3. If no match → read `docs/README.md` to understand the full doc structure, route from there.
4. If multiple docs are relevant → read the most relevant one first, offer to read others if needed.

## SKILL.md Body Outline

1. **Overview** — what this skill does
2. **When to Use** — triggering conditions
3. **The Index** — instructions to read `index.md` first
4. **Routing Rules** — how to match questions to docs, handle multi-doc questions
5. **Fallback** — what to do when the index doesn't cover it
6. **Updating the Index** — note that when docs are added/renamed, `index.md` should be updated

## Key Design Decisions

- **Index is authoritative for routing**, not the skill body — keeps the skill small.
- **Skill reads docs at runtime**, so answers are always current (not cached summaries).
- **Fallback to `docs/README.md`** handles anything the index misses.
- **No summarization logic in the skill** — it just routes and reads; the model does the understanding.

## Maintenance

When docs are added, renamed, or removed, update `index.md` to match. The skill
description does not mention this — it's a developer-facing concern, not a
model-facing one.
