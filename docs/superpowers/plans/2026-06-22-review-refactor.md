# Plan: Review-Driven Refactoring

Three independent refactoring tasks from the codebase review, addressing complexity, type safety, and duplication.

## Global Constraints

- Python ≥3.10, pyright basic, ruff (E, F, I), 100-char line length
- All existing tests must continue passing
- Do not change public API signatures (these are internal refactors)
- Follow existing patterns: `from __future__ import annotations` where already used, `# noqa: BLE001` on intentional bare excepts

## Tasks

### Task 1: Extract helpers from `run_turn`

**Problem:** `Harness.run_turn` (agent.py:445-571) is 125 lines with 3 nesting levels. It handles prompt assembly, attachment injection, hook interception (nested async closure), tool-call self-healing, the main agent.run loop, deferred-tool approval rounds, rollback on interrupt, error persistence, compaction, and autonaming.

**What to extract:**
1. `_build_hooked_handler(self, base_handler)` — lines 460-478: builds the hook-intercepting event stream wrapper. Returns the handler (or None). Moves the nested `_hooked_handler` closure to a named method.
2. `_run_with_approval(self, user_prompt, deferred_results, toolsets, event_stream_handler, resumable)` — lines 493-571: the while-True loop that runs agent.run, handles DeferredToolRequests approval rounds, persists on success, and rolls back on interrupt. This is the core loop.

After extraction, `run_turn` should be ~40 lines: compact, attach, heal, delegate to the loop, persist/compact/autoname.

**Files:** `src/marim_harness/agent.py`

### Task 2: Type `SubagentRunner.__init__` and parameterize bare generics

**Problem:** `SubagentRunner.__init__` (subagents.py:58-78) has 7 untyped positional parameters. Additionally, bare `list`/`dict` generics throughout the codebase lose static type information.

**What to type:**
1. `SubagentRunner.__init__` — add type annotations for all parameters:
   - `provider: ToolProvider`
   - `mcp: McpManager`
   - `deps: Deps`
   - `hooks: TurnHooks`
   - `session: SessionController`
   - `get_model: Callable[[], Model]` (or `Callable[[], Any]` if Model is not importable)
   - `model_settings: ModelSettings | None = None`
   - `request_limit: int = 50`
   - `build_model: Callable[[str], Any] | None = None`
2. Parameterize bare generics across these specific instances:
   - `agent.py:70` — `_has_unanswered_tool_calls(history: list)` → `list[ModelMessage]` (or appropriate pydantic_ai type)
   - `agent.py:94` — `_repair_unanswered_tool_calls(history: list) -> list` → typed
   - `agent.py:225` — `mcp_servers: list` in HarnessConfig → parameterize
   - `session/ctrl.py:47` — `self.history: list` → parameterize
   - `mcp/manager.py:14-16` — `servers: list`, `self.mcp_servers: list`, `self._live_servers: list` → parameterize
   - `compaction.py:38,211` — `Summarizer = Callable[[list[Any]], ...]` → `list[ModelMessage]`

Use `TYPE_CHECKING` guards where needed to avoid import cycles (follow existing pattern in deps.py/agent.py).

**Files:** `src/marim_harness/subagents.py`, `src/marim_harness/agent.py`, `src/marim_harness/session/ctrl.py`, `src/marim_harness/mcp/manager.py`, `src/marim_harness/compaction.py`

### Task 3: Deduplicate `run`/`run_background` and `_truncate`

**Problem:** Two instances of structural duplication:
1. `SubagentRunner.run` (subagents.py:223-281) and `run_background` (subagents.py:283-346) share ~80% of their code: open worktree → build → MCP grant → hooks start → run → hooks stop → usage → cap output → close worktree.
2. `_truncate` exists in both `errors.py:44-45` (simple head truncation) and `tools/shell.py:12-24` (middle truncation with marker).

**What to do:**
1. Extract a shared `_execute_spawn` method on `SubagentRunner` that handles the common lifecycle (worktree open, build, grant, hooks, run, hooks stop, usage, cap, worktree close). The differences:
   - `run`: has `stream_id`, event handler from `self.handler(stream_id)`, error containment (catch → return error string), foreground usage fold
   - `run_background`: no `stream_id`, handler from `self.handler(None)`, error re-raises, background usage persist immediately, separate `_bg_seq` for spill naming
   
   The shared method should accept a `background: bool` parameter and handle both paths. The error handling difference (contain vs re-raise) and usage persistence difference can be conditional.

2. For `_truncate`: these are genuinely different functions serving different purposes (errors.py truncates from the end; shell.py truncates from the middle). The review flagged them as sharing a name. Rename the shell.py version to `_truncate_middle` to make the distinction explicit and avoid confusion. Do NOT unify them into one function — they have different semantics.

**Files:** `src/marim_harness/subagents.py`, `src/marim_harness/tools/shell.py`
