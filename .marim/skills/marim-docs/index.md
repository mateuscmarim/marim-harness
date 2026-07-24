# Marim Documentation Index

Curated index of every Marim documentation file, mapped to purpose, keywords, and example trigger questions.

## Root docs

#### `README.md`
**Purpose:** Project overview, features, install, usage, config
**Topics:** install, setup, provider, model, env vars, features, quickstart
**When to ask:** "what is marim?", "how do I install?", "what features does it have?"

#### `CLAUDE.md`
**Purpose:** Project instructions for Claude Code: architecture, conventions, commands
**Topics:** architecture, turn loop, deps, services, tools, conventions, ruff, pyright, build
**When to ask:** "how is the codebase structured?", "what are the conventions?", "how does the turn loop work?"

#### `CONTRIBUTING.md`
**Purpose:** Dev setup, conventions, PR checklist
**Topics:** dev setup, uv, pytest, ruff, pyright, PR, CI
**When to ask:** "how do I contribute?", "what's the dev setup?", "PR checklist"

#### `coding-guidelines.md`
**Purpose:** Code style rules and design principles
**Topics:** complexity, cohesion, naming, encapsulation, C901, design principles
**When to ask:** "coding style", "design principles", "how should I write code?"

#### `SECURITY.md`
**Purpose:** Trust and permission model
**Topics:** trust, permissions, modes, approval, security
**When to ask:** "how does trust work?", "permission model", "security"

#### `CHANGELOG.md`
**Purpose:** Release history
**Topics:** releases, breaking changes, versions
**When to ask:** "what changed in version X?", "release history", "breaking changes"

#### `ROADMAP.md`
**Purpose:** Project direction
**Topics:** roadmap, future, planned features
**When to ask:** "what's planned?", "roadmap", "future direction"

## Guides

#### `docs/guides/tui.md`
**Purpose:** Interactive TUI: slash commands, key bindings, approvals, sub-agents screen, settings
**Topics:** TUI, keybindings, slash commands, approvals, settings, interactive
**When to ask:** "TUI shortcuts", "slash commands", "key bindings", "how do approvals work?"

#### `docs/guides/headless.md`
**Purpose:** One-shot mode for scripts and CI: output formats, exit codes, management subcommands
**Topics:** headless, CI, one-shot, exit codes, output format, scripts
**When to ask:** "headless mode", "CI integration", "one-shot mode"

#### `docs/guides/sessions.md`
**Purpose:** Sessions, resuming, compaction, checkpoints, /rewind
**Topics:** session, compaction, checkpoint, resume, rewind, autoname
**When to ask:** "how do sessions work?", "what is compaction?", "/rewind", "checkpoint"

#### `docs/guides/subagents.md`
**Purpose:** Sub-agents and background jobs: spawning, agent specs, model tiers, claude-cli backend
**Topics:** sub-agents, spawn, tiers, background jobs, claude-cli, nesting
**When to ask:** "sub-agents", "how do I spawn?", "model tiers", "background jobs"

#### `docs/guides/workflows.md`
**Purpose:** Dynamic workflows: sandboxed orchestration scripts, host functions, budgets, safety
**Topics:** workflows, run_workflow, monty, sandbox, agent(), orchestration
**When to ask:** "dynamic workflows", "run_workflow", "orchestration scripts"

#### `docs/guides/hooks.md`
**Purpose:** Lifecycle hooks: every event, config format, context injection, Claude Code compatibility
**Topics:** hooks, lifecycle, SessionStart, PreCompact, AGENTS.md, events
**When to ask:** "hooks", "lifecycle events", "how do hooks work?"

#### `docs/guides/mcp.md`
**Purpose:** MCP servers: config scopes, marim mcp CLI, trust, tool exposure, sub-agent grants
**Topics:** MCP, servers, config, trust, tool grants, sub-agent grants
**When to ask:** "MCP servers", "how do I add an MCP server?", "tool exposure"

#### `docs/guides/skills-and-memory.md`
**Purpose:** AGENTS.md instructions, skills, persistent memory, session scratchpad
**Topics:** skills, memory, AGENTS.md, scratchpad, recall, remember
**When to ask:** "skills", "persistent memory", "AGENTS.md", "scratchpad"

#### `docs/guides/trust.md`
**Purpose:** Trust and permission model: modes, command policy, path guards, project trust gate
**Topics:** trust, modes, ask, auto, plan, command policy, path guards
**When to ask:** "trust model", "what is ask mode?", "command policy", "path guards"

## Reference & SDK

#### `docs/reference/configuration.md`
**Purpose:** Every MARIM_* env var with defaults and formats
**Topics:** env vars, MARIM_PROVIDER, MARIM_MODEL, configuration, defaults
**When to ask:** "env vars", "configuration", "MARIM_* variables", "how do I configure?"

#### `docs/reference/serve-api.md`
**Purpose:** marim serve HTTP daemon: REST endpoints, streaming, auth, lifecycle
**Topics:** serve, HTTP, REST, API, WebSocket, daemon
**When to ask:** "marim serve", "HTTP API", "REST endpoints"

#### `docs/sdk/README.md`
**Purpose:** SDK overview
**Topics:** SDK, embedding, HarnessBuilder, overview
**When to ask:** "SDK overview", "embedding SDK"

#### `docs/sdk/builder.md`
**Purpose:** HarnessBuilder with_* methods
**Topics:** HarnessBuilder, with_model, with_tools, with_session, builder API
**When to ask:** "HarnessBuilder", "with_* methods", "how to compose the agent"

#### `docs/sdk/capabilities.md`
**Purpose:** pydantic-ai capabilities marim ships
**Topics:** capabilities, pydantic-ai, model settings, features
**When to ask:** "capabilities", "pydantic-ai features"

#### `docs/sdk/custom-tools.md`
**Purpose:** Tool signature, gating, collisions
**Topics:** tools, custom tools, requires_approval, tool gating, collisions
**When to ask:** "custom tools", "how do tools work?", "tool gating"

#### `docs/sdk/getting-started.md`
**Purpose:** Install, quickstart
**Topics:** install, quickstart, first run, setup
**When to ask:** "getting started", "quickstart", "first steps"

#### `docs/sdk/integrations.md`
**Purpose:** MCP, LSP, forge, hooks, bash policy
**Topics:** integrations, MCP, LSP, forge, hooks, bash policy
**When to ask:** "integrations", "MCP setup", "LSP", "forge"

#### `docs/sdk/sessions-and-state.md`
**Purpose:** What touches disk
**Topics:** sessions, disk, state, storage, persistence
**When to ask:** "what touches disk?", "session storage", "state management"

#### `docs/sdk/subagents.md`
**Purpose:** AgentDef, grants, depth ceiling
**Topics:** AgentDef, sub-agents, grants, depth, nesting
**When to ask:** "AgentDef", "sub-agent grants", "depth ceiling"

#### `docs/sdk/testing.md`
**Purpose:** Network-free turn tests
**Topics:** testing, tests, mock, network-free, fixtures
**When to ask:** "how to test", "testing guide", "network-free tests"

#### `docs/sdk/turns.md`
**Purpose:** run_turn, approval, Mode, streaming
**Topics:** run_turn, approval, Mode, streaming, turn loop
**When to ask:** "run_turn", "how turns work", "approval flow", "Mode"

#### `docs/sdk/tutorial-daily-report.md`
**Purpose:** End-to-end walkthrough
**Topics:** tutorial, daily report, walkthrough, example
**When to ask:** "tutorial", "daily report example", "end-to-end walkthrough"

## Other docs

#### `docs/architecture.md`
**Purpose:** Codebase map and invariants
**Topics:** architecture, invariants, turn loop, deps, services, structure
**When to ask:** "architecture", "codebase structure", "how is it organized?"

#### `docs/embedding.md`
**Purpose:** HarnessBuilder quickstart
**Topics:** embedding, HarnessBuilder, quickstart, library
**When to ask:** "embedding quickstart", "using HarnessBuilder"

#### `docs/plugins.md`
**Purpose:** Authoring plugins: skills, sub-agents, hooks, MCP servers, AGENTS.md
**Topics:** plugins, authoring, skills, hooks, MCP
**When to ask:** "plugins", "how do I create a plugin?", "authoring plugins"

#### `docs/lsp-plugins.md`
**Purpose:** Adding language-server support via LSP provider plugins
**Topics:** LSP, language server, plugins, providers, diagnostics
**When to ask:** "LSP plugins", "adding language support", "LSP providers"
