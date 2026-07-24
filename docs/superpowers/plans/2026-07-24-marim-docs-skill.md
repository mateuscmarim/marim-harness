# marim-docs Skill Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Create a project-scoped skill at `.marim/skills/marim-docs/` that routes questions to the right Marim documentation file using a curated static index.

**Architecture:** Two markdown files — `SKILL.md` (skill instructions) and `index.md` (curated doc index with purpose, topics, and trigger questions for each doc). The skill matches user questions against the index, reads the relevant doc(s) at runtime, and answers from current content. Falls back to `docs/README.md` when the index doesn't cover a question.

**Tech Stack:** Markdown only — no code, no tests.

## Global Constraints

- Skill lives at `.marim/skills/marim-docs/` (project-scoped, requires `MARIM_TRUST_PROJECT_HOOKS=1`)
- Follows the agentskills.io SKILL.md format: YAML frontmatter (`name`, `description`) + markdown body
- Index covers all docs listed in `docs/README.md` plus root-level `.md` files
- Skill reads docs at runtime via `read_file` — no cached summaries

---

## File Structure

```
.marim/skills/marim-docs/
  SKILL.md       — skill instructions (routing rules, workflow, fallback)
  index.md       — curated doc index (purpose, topics, trigger questions per doc)
```

---

### Task 1: Create the curated doc index

**Files:**
- Create: `.marim/skills/marim-docs/index.md`

**Interfaces:**
- Consumes: `docs/README.md` (master doc index with summaries), root-level `.md` files
- Produces: `index.md` — read by `SKILL.md` at skill invocation time

- [ ] **Step 1: Create the directory**

```bash
mkdir -p .marim/skills/marim-docs
```

- [ ] **Step 2: Write `index.md`**

Create `.marim/skills/marim-docs/index.md` with three sections. Each entry follows this format:

```markdown
### `<path>`
**Purpose:** <one-line summary>
**Topics:** <comma-separated keywords>
**When to ask:** "<example trigger question 1>", "<example trigger question 2>"
```

The three sections and their entries:

**Root docs** (7 entries):
- `README.md` — project overview, features, install, usage, config. Topics: install, setup, provider, model, env vars, features. Triggers: "what is marim?", "how do I install?", "what features does it have?"
- `CLAUDE.md` — project instructions for Claude Code: architecture, conventions, commands. Topics: architecture, turn loop, deps, services, tools, conventions, ruff, pyright. Triggers: "how is the codebase structured?", "what are the conventions?", "how does the turn loop work?"
- `CONTRIBUTING.md` — dev setup, conventions, PR checklist. Topics: dev setup, uv, pytest, ruff, pyright, PR. Triggers: "how do I contribute?", "what's the dev setup?", "PR checklist"
- `coding-guidelines.md` — code style rules and design principles. Topics: complexity, cohesion, naming, encapsulation, C901. Triggers: "coding style", "design principles", "how should I write code?"
- `SECURITY.md` — trust and permission model. Topics: trust, permissions, modes, approval. Triggers: "how does trust work?", "permission model", "security"
- `CHANGELOG.md` — release history. Topics: releases, breaking changes, versions. Triggers: "what changed in version X?", "release history", "breaking changes"
- `ROADMAP.md` — project direction. Topics: roadmap, future, planned features. Triggers: "what's planned?", "roadmap", "future direction"

**Guides** (9 entries):
- `docs/guides/tui.md` — interactive TUI: slash commands, key bindings, approvals, sub-agents screen, settings. Topics: TUI, keybindings, slash commands, approvals, settings. Triggers: "TUI shortcuts", "slash commands", "key bindings", "how do approvals work?"
- `docs/guides/headless.md` — one-shot mode for scripts and CI: output formats, exit codes, management subcommands. Topics: headless, CI, one-shot, exit codes, output format. Triggers: "headless mode", "CI integration", "one-shot mode"
- `docs/guides/sessions.md` — sessions, resuming, compaction, checkpoints, /rewind. Topics: session, compaction, checkpoint, resume, rewind, autoname. Triggers: "how do sessions work?", "what is compaction?", "/rewind", "checkpoint"
- `docs/guides/subagents.md` — sub-agents and background jobs: spawning, agent specs, model tiers, claude-cli backend. Topics: sub-agents, spawn, tiers, background jobs, claude-cli. Triggers: "sub-agents", "how do I spawn?", "model tiers", "background jobs"
- `docs/guides/workflows.md` — dynamic workflows: sandboxed orchestration scripts, host functions, budgets, safety. Topics: workflows, run_workflow, monty, sandbox, agent(). Triggers: "dynamic workflows", "run_workflow", "orchestration scripts"
- `docs/guides/hooks.md` — lifecycle hooks: every event, config format, context injection, Claude Code compatibility. Topics: hooks, lifecycle, SessionStart, PreCompact, AGENTS.md. Triggers: "hooks", "lifecycle events", "how do hooks work?"
- `docs/guides/mcp.md` — MCP servers: config scopes, marim mcp CLI, trust, tool exposure, sub-agent grants. Topics: MCP, servers, config, trust, tool grants. Triggers: "MCP servers", "how do I add an MCP server?", "tool exposure"
- `docs/guides/skills-and-memory.md` — AGENTS.md instructions, skills, persistent memory, session scratchpad. Topics: skills, memory, AGENTS.md, scratchpad, recall, remember. Triggers: "skills", "persistent memory", "AGENTS.md", "scratchpad"
- `docs/guides/trust.md` — trust and permission model: modes, command policy, path guards, project trust gate. Topics: trust, modes, ask, auto, plan, command policy, path guards. Triggers: "trust model", "what is ask mode?", "command policy", "path guards"

**Reference & SDK** (15 entries):
- `docs/reference/configuration.md` — every MARIM_* env var with defaults and formats. Topics: env vars, MARIM_PROVIDER, MARIM_MODEL, configuration. Triggers: "env vars", "configuration", "MARIM_* variables", "how do I configure?"
- `docs/reference/serve-api.md` — marim serve HTTP daemon: REST endpoints, streaming, auth, lifecycle. Topics: serve, HTTP, REST, API, WebSocket. Triggers: "marim serve", "HTTP API", "REST endpoints"
- `docs/sdk/README.md` — SDK overview. Topics: SDK, embedding, HarnessBuilder. Triggers: "SDK overview", "embedding SDK"
- `docs/sdk/builder.md` — HarnessBuilder with_* methods. Topics: HarnessBuilder, with_model, with_tools, with_session. Triggers: "HarnessBuilder", "with_* methods", "how to compose the agent"
- `docs/sdk/capabilities.md` — pydantic-ai capabilities marim ships. Topics: capabilities, pydantic-ai, model settings. Triggers: "capabilities", "pydantic-ai features"
- `docs/sdk/custom-tools.md` — tool signature, gating, collisions. Topics: tools, custom tools, requires_approval, tool gating. Triggers: "custom tools", "how do tools work?", "tool gating"
- `docs/sdk/getting-started.md` — install, quickstart. Topics: install, quickstart, first run. Triggers: "getting started", "quickstart", "first steps"
- `docs/sdk/integrations.md` — MCP, LSP, forge, hooks, bash policy. Topics: integrations, MCP, LSP, forge, hooks. Triggers: "integrations", "MCP setup", "LSP", "forge"
- `docs/sdk/sessions-and-state.md` — what touches disk. Topics: sessions, disk, state, storage. Triggers: "what touches disk?", "session storage", "state management"
- `docs/sdk/subagents.md` — AgentDef, grants, depth ceiling. Topics: AgentDef, sub-agents, grants, depth. Triggers: "AgentDef", "sub-agent grants", "depth ceiling"
- `docs/sdk/testing.md` — network-free turn tests. Topics: testing, tests, mock, network-free. Triggers: "how to test", "testing guide", "network-free tests"
- `docs/sdk/turns.md` — run_turn, approval, Mode, streaming. Topics: run_turn, approval, Mode, streaming, turn loop. Triggers: "run_turn", "how turns work", "approval flow", "Mode"
- `docs/sdk/tutorial-daily-report.md` — end-to-end walkthrough. Topics: tutorial, daily report, walkthrough. Triggers: "tutorial", "daily report example", "end-to-end walkthrough"

**Other docs** (4 entries):
- `docs/architecture.md` — codebase map and invariants. Topics: architecture, invariants, turn loop, deps, services. Triggers: "architecture", "codebase structure", "how is it organized?"
- `docs/embedding.md` — HarnessBuilder quickstart. Topics: embedding, HarnessBuilder, quickstart. Triggers: "embedding quickstart", "using HarnessBuilder"
- `docs/plugins.md` — authoring plugins: skills, sub-agents, hooks, MCP servers, AGENTS.md. Topics: plugins, authoring, skills, hooks. Triggers: "plugins", "how do I create a plugin?", "authoring plugins"
- `docs/lsp-plugins.md` — adding language-server support via LSP provider plugins. Topics: LSP, language server, plugins, providers. Triggers: "LSP plugins", "adding language support", "LSP providers"

- [ ] **Step 3: Verify the index covers all docs**

Read `docs/README.md` and confirm every doc listed there (plus root-level `.md` files) has an entry in `index.md`. If any are missing, add them.

- [ ] **Step 4: Commit**

```bash
git add .marim/skills/marim-docs/index.md
git commit -m "docs: add marim-docs skill index"
```

---

### Task 2: Create the skill instructions

**Files:**
- Create: `.marim/skills/marim-docs/SKILL.md`

**Interfaces:**
- Consumes: `index.md` (created in Task 1)
- Produces: The skill itself — loaded by marim's skill discovery at `.marim/skills/marim-docs/SKILL.md`

- [ ] **Step 1: Write `SKILL.md`**

Create `.marim/skills/marim-docs/SKILL.md` with this content:

```markdown
---
name: marim-docs
description: Use when the user asks a question about how Marim works, what a feature does, how to configure something, what docs cover a topic, wants an overview of the system, or needs to find relevant docs for a task they're working on.
---

# marim-docs

## Overview

This skill navigates and reads Marim's documentation to answer questions about the
project. It uses a curated index for fast routing and reads docs at runtime so
answers are always current.

## When to Use

Activate this skill when the user:
- Asks how a Marim feature works ("how do sessions work?", "what is compaction?")
- Asks how to configure something ("what env vars control the model?", "how do I set up MCP?")
- Wants an overview of the system or a subsystem
- Needs to find which docs are relevant for a task they're working on
- Asks about commands, slash commands, CLI flags, or API endpoints

Do NOT activate for:
- Questions about the source code implementation (use grep/read_file directly)
- Questions about bugs or unexpected behavior (use superpowers:systematic-debugging)
- General coding questions unrelated to Marim

## The Index

Read `index.md` (in this same directory) first. It maps every Marim doc to its
purpose, key topics, and example trigger questions.

## Routing Rules

1. **Match the user's question** against the index entries — look at the "Topics"
   and "When to ask" fields for each doc.

2. **If a match is found:** Read the matched doc(s) using `read_file` (with the
   workspace-relative path from the index). Answer the user's question from the
   doc content.

3. **If multiple docs match:** Read the most relevant one first. If the answer
   isn't complete, offer to read the others: "I found a few relevant docs — want
   me to also check [doc]?"

4. **If no match is found:** Fall back to reading `docs/README.md` to see the
   full doc structure, then route from there.

5. **For overviews:** Read 2-3 key docs (usually `docs/architecture.md` plus
   the most relevant guide) and synthesize.

## Fallback

When the index doesn't cover a question:
1. Read `docs/README.md` for the full doc listing
2. If still unclear, search the workspace with `grep` for relevant terms in `docs/`
3. If the question is about source code behavior rather than documented behavior,
   read the relevant source file directly

## Updating the Index

When docs are added to, renamed in, or removed from the project, update `index.md`
to match. Keep the index in sync with `docs/README.md` as the source of truth.
```

- [ ] **Step 2: Verify the skill loads correctly**

Confirm:
- `.marim/skills/marim-docs/SKILL.md` has valid YAML frontmatter (name + description)
- `.marim/skills/marim-docs/index.md` exists and is referenced correctly in SKILL.md
- The description triggers on the intended use cases

- [ ] **Step 3: Commit**

```bash
git add .marim/skills/marim-docs/SKILL.md
git commit -m "feat: add marim-docs skill for navigating documentation"
```

---

### Task 3: Verify the skill in context

- [ ] **Step 1: Test that marim discovers the skill**

Run marim in the project directory and verify `marim-docs` appears in the skills list. If using the TUI, check the skills screen. If headless:

```bash
MARIM_DEBUG=1 uv run marim --help 2>&1 | grep -i skill
```

- [ ] **Step 2: Smoke-test a lookup**

Ask a question that should trigger the skill (e.g., "how do sessions work?") and verify it routes to `docs/guides/sessions.md` and reads it.

- [ ] **Step 3: Smoke-test the fallback**

Ask a question not covered by the index (e.g., something very specific about internal implementation) and verify it falls back to `docs/README.md`.

- [ ] **Step 4: Final commit (if any fixes were needed)**

```bash
git add .marim/skills/marim-docs/
git commit -m "fix: adjust marim-docs skill after smoke testing"
```
