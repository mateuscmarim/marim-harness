# marim-harness — Design Spec

*Date: 2026-06-15 · Status: approved design, pre-implementation · Working name: `marim-harness`*

## 1. Purpose

A terminal coding agent: a Textual TUI driving a Pydantic AI agent that can read,
search, and modify a real codebase, with mode-based approval gating.

**Primary goal:** learn by building — understand a coding harness end-to-end
(agent loop, tools, permissions, TUI). Correctness and clear component
boundaries matter more than feature breadth.

**Secondary goal:** evolve into a specialized harness, possibly a daily driver.
The architecture is therefore built to extend, but v1 builds only the v1 scope.

### Guiding principles
- **Use core Pydantic AI built-ins; reinvent nothing it provides.** Tool schemas,
  the agent loop, approval/deferred tools, retries, streaming, history, usage,
  and testing all come from the framework.
- **Hand-write only what is educational or not yet available** (see §8).
- **Decouple the agent core from the TUI** so the core is testable headless and
  the frontend is swappable.

## 2. Scope

### In scope (v1)
- Single agent, single session, **no persistence**.
- Tools: `read_file`, `write_file`, `edit_file`, `glob`, `grep`, `bash`.
- Mode-based permissions: `ask` / `auto` / `plan`.
- Streaming Textual TUI: single-pane conversation with collapsible tool calls,
  input box, status bar, modal approval.
- Config-driven model: OpenRouter default, local-model support.

### Out of scope (v1, noted for later)
- Session persistence / resume.
- Sub-agents / task delegation.
- Context trimming / compaction.
- `CodeMode` / sandboxed execution (wrong primitive for editing a real repo).
- Adopting `pydantic-ai-harness` capabilities (not yet shipped — see §8).

## 3. Dependency landscape (researched 2026-06-15)

- **`pydantic-ai` 1.107.0** (latest). The skill bundled with this environment
  describes the 1.x API surface; verified against the installed package.
- **`pydantic-ai-harness` 0.3.0** ships **only `CodeMode`** today. Filesystem,
  Shell, approvals/guardrails, context-management, and session persistence are
  **roadmap, not released** (confirmed by introspecting the installed package:
  `__all__ == ['CodeMode']`, only `code_mode` submodule on disk). The README
  capability matrix describes the roadmap, not shipped code.
- **`textual`** for the TUI.

**Consequence:** the fs/shell tools must be hand-written for v1 regardless. This
matches the learn-by-building goal. We build them behind a swappable interface so
harness capabilities can be adopted later (§8).

## 4. Architecture

### Module structure
```
marim_harness/
├── config.py        # load model/provider/base_url/api_key + defaults (env + TOML)
├── deps.py          # Deps dataclass (workspace root, mutable mode, approval channel)
├── agent.py         # builds the Agent, registers the tool provider, owns the run
├── tools/
│   ├── provider.py  # ToolProvider protocol (swap point for future harness caps)
│   ├── fs.py        # read_file, write_file, edit_file, glob, grep
│   └── shell.py     # bash
├── workspace.py     # workspace-root path confinement (shared by all tools)
├── permissions.py   # Mode enum + resolver (ask/auto/plan → approve/deny/prompt)
├── tui/
│   ├── app.py       # Textual App, layout, status bar, input, mode keybinding
│   ├── widgets.py   # tool-call Collapsible widget, message widgets
│   └── approval.py  # modal approval screen (ModalScreen)
└── __main__.py      # wire config → agent → TUI, run
```

### The key boundary: agent ↔ UI
The agent core **never imports Textual**. The TUI drives the agent by consuming
Pydantic AI's **native** typed event stream from `agent.run_stream_events(...)`:
- `PartStartEvent` / `PartDeltaEvent` → streaming assistant text
- `FunctionToolCallEvent` → a tool call started
- `FunctionToolResultEvent` → a tool call finished

There is **no custom event hierarchy**. A small (~10-line) helper maps an event to
"which widget to create/update." This keeps the boundary while writing almost no
glue, and enables a future headless/CLI frontend with no core changes.

### The swap point: `ToolProvider`
`tools/provider.py` defines a `ToolProvider` protocol whose single job is to
register the harness's tools onto an `Agent`. v1 ships `BuiltinToolProvider`
(hand-written fs + shell). When `pydantic-ai-harness` ships FileSystem/Shell/
ToolGuard, a `HarnessToolProvider` can replace it without touching the TUI,
the loop, or the permission resolver.

## 5. The agent loop & permissions (the Pydantic AI core)

1. User submits a message → TUI calls `agent.run_stream_events(prompt,
   message_history=history, deps=deps)`.
2. Read tools (`read_file`, `glob`, `grep`) run freely. Mutating tools
   (`write_file`, `edit_file`, `bash`) are registered with
   `requires_approval=True`.
3. When approval is needed, Pydantic AI surfaces a `DeferredToolRequests`. The
   **single permission resolver** in `permissions.py` inspects `deps.mode`:
   - **`auto`** → approve immediately, no UI.
   - **`plan`** → `ToolDenied("read-only plan mode")`; the model receives the
     denial as feedback and adapts.
   - **`ask`** → emit an approval request to the TUI; the user's choice becomes
     `approve` or `ToolDenied(reason)`.
4. Resolved results feed back via `deferred_tool_results=DeferredToolResults(...)`
   and the run continues until completion.

**Mode is the only branch.** One approval code path, three outcomes.

Session history is maintained with `result.all_messages()` fed back as
`message_history=` — no custom history store. Token counts in the status bar come
from `result.usage()` (`RunUsage`) — we do not count tokens ourselves.

## 6. Tools

All tools are plain Python functions registered via `@agent.tool` (with
`RunContext[Deps]`) or `@agent.tool_plain`. **Schemas are generated from type
hints + docstrings** — we write no JSON schema. Tool and argument descriptions
live in docstrings.

All filesystem paths route through `workspace.py`, which resolves the path and
verifies it is inside the workspace root. A path outside the root raises
`ModelRetry("path outside workspace")` so the model self-corrects.

| Tool | Approval | Behavior |
|---|---|---|
| `read_file(path)` | none | Return file contents (with line numbers). |
| `glob(pattern)` | none | Return matching paths under the workspace. |
| `grep(pattern, path?)` | none | Return matching lines with locations. |
| `write_file(path, content)` | required | Create/overwrite a file. |
| `edit_file(path, old, new)` | required | **Exact string replace.** |
| `bash(command)` | required | Run a shell command. |

### `edit_file` — exact string replace
The model supplies `old_string` and `new_string`. The tool replaces the **unique**
occurrence of `old_string`. On **0 matches** or **>1 matches**, raise
`ModelRetry(...)` describing the problem — Pydantic AI feeds the error back and
the model retries with more context. *This `ModelRetry` flow is our edit error
handling; we do not build a correction loop.*

### `bash`
Runs via subprocess with `cwd` = workspace root. Uses the Pydantic AI tool
**`timeout=`** parameter (built-in) rather than a hand-rolled timeout. stdout and
stderr are captured; output is truncated at a fixed cap with a clear
"(truncated)" marker.

## 7. TUI (Textual)

Layout: **single-pane + collapsibles**.
- A `VerticalScroll` of message and tool-call widgets; an `Input` at the bottom;
  a custom status bar showing `mode · model · token count`.
- Each tool call is wrapped in Textual's built-in **`Collapsible`** widget:
  collapsed shows one summary line (name, args summary, status/result);
  expanded shows full args, output, or diff.
- Streaming assistant text appends live to the current assistant widget from
  `PartDeltaEvent`.
- `tui/approval.py` is a Textual **`ModalScreen`** with Approve / Deny, shown when
  the resolver requests approval in `ask` mode.
- Mode is cycled with a key binding (e.g. `ctrl+m`), updating `deps.mode` and the
  status bar.

## 8. Future: adopt harness capabilities (the migration path)

As `pydantic-ai-harness` capabilities graduate from roadmap to release, replace
hand-written parts via the `ToolProvider` swap point and core capability slots —
no TUI or loop changes:

| Hand-written now | Future harness capability |
|---|---|
| `tools/fs.py` + `workspace.py` confinement | **FileSystem** ("path traversal prevention") |
| `tools/shell.py` + `bash` timeout | **Shell** ("allowlists, denylists, timeouts") |
| `permissions.py` mode resolver | **ToolGuard / Approval workflows / `READONLY_RULESET`** |
| (out of scope v1) | **Sliding window / compaction / tool-output truncation** via `capabilities=[ProcessHistory(...)]` |
| (out of scope v1) | **Session persistence** |

Track: <https://github.com/pydantic/pydantic-ai-harness/issues/52>.

## 9. Error handling

- **Recoverable model mistakes** (edit no/many matches, path outside workspace,
  bad tool args) → `ModelRetry`; the model self-corrects. Tools that warrant it
  get a small `retries=` budget.
- **Provider / network errors** → surfaced to the TUI as an error message; the
  session stays alive. Optional `FallbackModel` (OpenRouter → local) is a
  config-level lever, not v1-default.
- **Unexpected model behavior** → wrap runs so `UnexpectedModelBehavior` is caught
  and shown; use `capture_run_messages()` during development to inspect the exact
  request/response history of a failed run.

## 10. Testing & observability

- **Tools** are pure functions over a temp workspace → direct unit tests
  (read/write/edit/glob/grep/bash, plus workspace-confinement rejection).
- **Permission resolver** → unit tests asserting each mode's outcome.
- **Agent behavior** → `FunctionModel` scripting exact tool calls (assert
  `edit_file` applies; assert `plan` mode denies mutation), `TestModel` for smoke.
  Always via `agent.override(model=...)`.
- **Observability** → optional **Logfire** behind a config flag
  (`instrument_pydantic_ai()`, `instrument_httpx(capture_all=True)`).

## 11. Project setup

- **`uv`** + `pyproject.toml`, Python **3.10+**.
- Runtime deps: `pydantic-ai` (1.107.x), `textual`.
- Dev deps: `pytest` + `anyio` (async tests), `ruff`.
- `pydantic-ai-harness` is **not** a v1 dependency (only `CodeMode` ships; unused).

## 12. Open questions / deferred decisions

- Final project name (`marim-harness` is a working name).
- Exact OpenRouter model default and local endpoint convention — settled in
  `config.py` at implementation time.
- Output truncation caps (bash output, large file reads) — concrete numbers
  chosen during implementation.
