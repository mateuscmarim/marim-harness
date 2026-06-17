# Design: marim-harness hooks engine (Claude-Code-compatible)

- **Date:** 2026-06-17
- **Status:** Approved (design); pending implementation plan
- **Goal:** Add a general lifecycle-hook engine to marim-harness that mirrors
  Claude Code's hook contract, so external tools — agentmemory in particular —
  integrate by configuration alone, running their existing CC hook scripts
  unmodified.

## Motivation

marim-harness has MCP support (a "plugin" surface) and a per-turn instruction
injection system, but **no lifecycle-hook engine**. Integrating agentmemory's
auto-capture (the mechanism that observes prompts/tool-use and injects recalled
context at session start) requires firing lifecycle events to external scripts.

Rather than build an agentmemory-specific bridge, we build a generic engine that
mirrors Claude Code's hook contract. This makes agentmemory a pure-config
integration and makes the engine reusable for any CC-compatible hook.

## Decisions (locked during brainstorming)

1. **Contract:** Mirror Claude Code — same event names, JSON-on-stdin payload
   schema, `hooks.json` block shape, and output protocol (exit codes +
   `additionalContext`). agentmemory's bundled `plugin/*` scripts run unmodified.
2. **Event scope:** Capture set + injection, **no veto**. Events:
   `SessionStart`, `UserPromptSubmit`, `PreToolUse`, `PostToolUse`,
   `PreCompact`, `SubagentStart`, `SubagentStop`, `Stop`, `SessionEnd`.
   Context injection (`additionalContext`) is honored only for `SessionStart`
   and `UserPromptSubmit`. CC's blocking/`decision:"block"`/exit-2 veto is
   intentionally ignored.
3. **Config source:** Dedicated `~/.config/marim/hooks.json` (global) and
   `.marim/hooks.json` (project). marim does **not** read `~/.claude/settings.json`.
4. **Trust:** Global config always runs. Project `.marim/hooks.json` is ignored
   unless `MARIM_TRUST_PROJECT_HOOKS` is truthy.
5. **Architecture:** Standalone `src/marim_harness/hooks/` subpackage, composed
   into existing seams (mirrors the `mcp/` subpackage shape). No internal
   event-bus refactor; no inlining into `agent.py`.

## Non-goals

- No veto / blocking semantics (no tool denial, no prompt blocking, no
  forced-continue).
- No reading of Claude Code's own `settings.json`.
- No agentmemory-specific code in the harness — agentmemory is wired via config.
- No new interactive trust-prompt UI (project hooks gate on an env flag only).

## Architecture

New subpackage `src/marim_harness/hooks/`:

### `events.py` (leaf module, no intra-package deps)

Event-name constants and the injection-eligible set. Kept dependency-free to
avoid import cycles (same lesson as `tools/names.py`).

```python
SESSION_START      = "SessionStart"
USER_PROMPT_SUBMIT = "UserPromptSubmit"
PRE_TOOL_USE       = "PreToolUse"
POST_TOOL_USE      = "PostToolUse"
PRE_COMPACT        = "PreCompact"
SUBAGENT_START     = "SubagentStart"
SUBAGENT_STOP      = "SubagentStop"
STOP               = "Stop"
SESSION_END        = "SessionEnd"

INJECTING_EVENTS = frozenset({SESSION_START, USER_PROMPT_SUBMIT})
```

### `config.py` (mirrors `mcp/config.py`)

```python
def global_hooks_config_path() -> Path        # ~/.config/marim/hooks.json
def project_hooks_config_path(root) -> Path    # <root>/.marim/hooks.json
def load_hooks_config(workspace_root, *, trust_project: bool) -> dict
```

- Reads the global file always; merges the project file **only if
  `trust_project`**.
- Returns the CC-shaped map under the `hooks` key:
  `{event: [{matcher?, hooks: [{type: "command", command, timeout?}]}]}`.
- Fail-soft: a missing or malformed file yields `{}` (never raises).
- Merge semantics: per-event lists from global and (trusted) project are
  concatenated; both sets of matching hooks run.

### `runner.py`

```python
class HookRunner:
    def __init__(self, config: dict) -> None
    async def dispatch(self, event: str, payload: dict) -> Optional[str]
```

- Selects entries for `event`; filters each by `matcher` (regex on
  `payload["tool_name"]`) for `PreToolUse`/`PostToolUse`; `matcher` is ignored
  for all other events. Absent/empty/`"*"` matcher matches everything.
- Runs each `command` with `asyncio.create_subprocess_shell(...,
  start_new_session=True)`, pipes `json.dumps(payload)` on **stdin**, captures
  stdout, enforces a per-hook `timeout` (default 30s) with process-group
  SIGKILL — reusing the exact discipline in `tools/shell.py`.
- For injection events, parses stdout and returns the concatenated
  `additionalContext` across matching hooks; for observe-only events returns
  `None`.
- **Never raises.** A failing/missing/timed-out hook is swallowed (and may be
  logged); it contributes no injected context.

## Config schema (`hooks.json`)

CC-identical, so existing CC hook blocks paste in directly:

```jsonc
{
  "hooks": {
    "SessionStart":     [{ "hooks": [{ "type": "command", "command": "$AM/plugin/session-start.sh" }] }],
    "UserPromptSubmit": [{ "hooks": [{ "type": "command", "command": "$AM/plugin/observe.sh" }] }],
    "PreToolUse":       [{ "matcher": "bash|edit_file|write_file",
                           "hooks": [{ "type": "command", "command": "$AM/plugin/pre-tool.sh", "timeout": 10 }] }],
    "PostToolUse":      [{ "matcher": "*", "hooks": [{ "type": "command", "command": "$AM/plugin/observe.sh" }] }]
  }
}
```

- Top-level `hooks` key matches CC's `settings.json` sub-object.
- `matcher` optional; regex on `tool_name`; absent/empty/`"*"` = all.
- `timeout` per hook in seconds, default 30 (marim's shell default).
- `${VAR}` / `$VAR` in `command` expand from the environment.
- Only `"type": "command"` hooks are supported; unknown types are skipped.

## Payload schema (stdin JSON, CC field names)

Common to every event: `hook_event_name`, `session_id` (from the session
store; empty string when storeless), `cwd` (workspace root), `transcript_path`
(the session `.json` path; empty when storeless).

| Event | Extra fields |
| --- | --- |
| `SessionStart` | `source` — `startup` \| `resume` \| `clear` |
| `UserPromptSubmit` | `prompt` |
| `PreToolUse` | `tool_name`, `tool_input` |
| `PostToolUse` | `tool_name`, `tool_input`, `tool_response` |
| `PreCompact` | `trigger` (`auto`), `custom_instructions` (`""`) |
| `SubagentStart` | `subagent_type`, `task` |
| `SubagentStop` | `subagent_type`, `task`, `result` |
| `Stop` | (common only) |
| `SessionEnd` | `reason` |

## Output / injection protocol

Hook stdout is consumed only on **exit 0**. For injection events
(`SessionStart`, `UserPromptSubmit`) the runner accepts either:

- JSON `{"hookSpecificOutput": {"additionalContext": "..."}}` (CC structured
  form), or
- plain non-JSON stdout text (used verbatim as the context to inject).

Contexts from multiple matching hooks are concatenated (newline-joined).
Non-zero exit, empty stdout, or unparseable-with-no-text output → no injection.
CC's `decision`/exit-2 veto fields are ignored (no-veto decision).

## Wiring into existing seams (the only edits to current code)

| Event | Fire point | Mechanism |
| --- | --- | --- |
| `SessionStart` | `Harness.resume` / `new_session` / initial build (`agent.py:135-157`) | dispatch once with `source`; stash returned context in `self._pending_hook_context`; prepend to the next `run_turn` prompt — same pattern as the jobs-digest / `_pending_error_note` prepend at `agent.py:291-298` |
| `UserPromptSubmit` | `run_turn` before `agent.run` (`agent.py:299-317`) | dispatch with `prompt`; prepend returned context to `user_prompt` |
| `PreToolUse` / `PostToolUse` | `run_turn`'s `event_stream_handler` | compose a wrapper that forwards to the existing handler and maps `FunctionToolCallEvent` → `PreToolUse`, `FunctionToolResultEvent` → `PostToolUse` (observe-only; no tool wrapping) |
| `PreCompact` | `SessionController.maybe_compact` (`ctrl.py:95`) | dispatch via `self.deps.hooks` when a compaction will run, before the trim |
| `SubagentStart` / `SubagentStop` | `_run_subagent` / `_run_background_subagent` (`agent.py:230-270`) | dispatch around `sub.run` |
| `Stop` | end of `run_turn` (`agent.py:358-361`) | dispatch after the final output |
| `SessionEnd` | TUI `on_unmount` + headless `finally` (`headless.py:97`) | dispatch on teardown |

Supporting wiring:

- `Deps.hooks: Optional[HookRunner] = None` (alongside `command_policy`,
  `deps.py:35`).
- Built in `bootstrap.py` from `load_hooks_config(...)`, gated on
  `MARIM_TRUST_PROJECT_HOOKS` (new env var parsed in `config/model.py`,
  following the `MARIM_PROACTIVE_MEMORY` template).
- When no config exists, `Deps.hooks` is `None` and every fire-point is a cheap
  no-op (a single `is None` check).

## Error handling & security

- **Fail-soft everywhere** — hooks never raise into a turn (codebase invariant).
  Timeout = process-group SIGKILL, treated as no output.
- **Trust** — project `.marim/hooks.json` ignored unless
  `MARIM_TRUST_PROJECT_HOOKS` is truthy; global config always runs. Documented
  in the README beside the existing MCP-trust caveats.
- **Performance** — injection/observe hooks are awaited under a bounded
  timeout; agentmemory's local POST is fast. `PreToolUse`/`PostToolUse` run
  inline in the event stream, so a slow hook there slows the turn — hence the
  per-hook `timeout` and the recommendation (in docs) of a short timeout for
  tool events.

## Testing (TDD)

- `tests/test_hooks_config.py` — global-only load; project gated by trust flag;
  malformed/missing fail-soft; global+project merge.
- `tests/test_hooks_runner.py` — real `tmp_path` shell scripts: payload-on-stdin
  round-trip; matcher filtering; `additionalContext` via both JSON and
  plain-stdout forms; multi-hook concatenation; timeout kill; non-zero exit
  swallowed; missing command swallowed.
- `tests/test_agent.py` additions — `SessionStart` context prepended once then
  consumed; `UserPromptSubmit` context prepended; `Pre`/`PostToolUse` fired via
  the composed event handler.
- All tests use real subprocesses + `FunctionModel`/`TestModel`, no mocks
  (matches the suite's existing discipline).

## agentmemory bridge (what this unlocks)

With the engine in place, agentmemory integration is **config + docs only**:

1. `agentmemory` MCP block in global `mcp.json` (the "plugin" layer — gives the
   model `memory_*` tools on demand).
2. A global `hooks.json` pointing `SessionStart` / `UserPromptSubmit` /
   `PostToolUse` at agentmemory's bundled `plugin/*` scripts (which run
   unmodified thanks to the CC contract), with the agentmemory server running on
   `:3111`.

Deliverable: an `examples/agentmemory/` directory with a ready-to-copy
`hooks.json` + `mcp.json` and a README section. No agentmemory-specific code in
the harness — the engine is generic.

> Note: exact agentmemory script names/ports (`@agentmemory/mcp`, `:3111`,
> `plugin/*` paths) come from agentmemory's README via a doc summary and should
> be verified against a running `agentmemory` install when authoring the
> `examples/agentmemory/` files.

## Open questions

None blocking. Future extensions (out of scope here): veto/blocking semantics;
reading `~/.claude/settings.json`; an interactive per-workspace trust prompt.
