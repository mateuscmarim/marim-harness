# agent.py Refactor Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Decompose the monolithic `Harness` class into `McpManager`, `SessionController`, and a `register_instructions` function, leaving a thin turn-runner `Harness` that delegates to them.

**Architecture:** Extract MCP lifecycle state/methods into `mcp_manager.py`, session lifecycle into `session_ctrl.py`, and the 7 instruction-closure registrations into `instructions.py`. `Harness` in `agent.py` shrinks to wiring + turn runner + sub-agent spawn. All existing tests pass unchanged — backward-compat properties bridge the old attribute names.

**Tech Stack:** Python 3.12+, pydantic-ai, anyio (tests), pytest

---

## File map

| File | Action |
|---|---|
| `src/marim_harness/compaction.py` | Extend — add `Titler`, `make_summarizer`, `make_titler`, `clean_title` |
| `src/marim_harness/mcp_manager.py` | **Create** — `McpManager` class |
| `src/marim_harness/session_ctrl.py` | **Create** — `SessionController` class |
| `src/marim_harness/instructions.py` | Extend — add `register_instructions` |
| `src/marim_harness/agent.py` | Refactor — thin `Harness` that wires the above |

No changes to test files, TUI, or CLI.

---

## Task 1: Move summarizer/titler utilities to `compaction.py`

**Files:**
- Modify: `src/marim_harness/compaction.py`
- Modify: `src/marim_harness/agent.py`

- [ ] **Step 1: Append to `compaction.py`** — add `Titler`, `make_summarizer`, `make_titler`, `clean_title` after the existing `compact_history_with_summary` function.

```python
# --- append to src/marim_harness/compaction.py ---

from pydantic_ai import Agent

Titler = Callable[[list], Awaitable[str]]

_SUMMARY_INSTRUCTIONS = (
    "You compress a coding-session transcript into a dense summary so the agent "
    "can keep working with less context. Preserve: the user's goals and "
    "constraints, decisions made, files read or edited and what changed, command "
    "results, and any unresolved problems or next steps. Drop pleasantries and "
    "redundant detail. Write terse notes, not prose."
)

_TITLE_INSTRUCTIONS = (
    "You write a short, specific title for a coding session from its transcript. "
    "Reply with the title only — no quotes, no trailing punctuation, at most six "
    "words. Name the concrete task, e.g. 'Fix the parser off-by-one' or 'Add "
    "session auto-naming'."
)

_MAX_TITLE_CHARS = 50


def make_summarizer(model) -> Summarizer:
    summary_agent = Agent(model, instructions=_SUMMARY_INSTRUCTIONS)

    async def summarize(messages: list) -> str:
        result = await summary_agent.run(render_transcript(messages))
        return result.output

    return summarize


def clean_title(raw: str) -> str:
    lines = [line.strip() for line in (raw or "").splitlines()]
    text = next((line for line in lines if line), "")
    if text.lower().startswith("title:"):
        text = text[len("title:"):].strip()
    text = text.strip("\"'`").strip().rstrip(".!?,;:").strip()
    if len(text) > _MAX_TITLE_CHARS:
        text = text[:_MAX_TITLE_CHARS].rstrip() + "…"
    return text or "Untitled session"


def make_titler(model) -> Titler:
    title_agent = Agent(model, instructions=_TITLE_INSTRUCTIONS)

    async def title(messages: list) -> str:
        result = await title_agent.run(render_transcript(messages))
        return clean_title(result.output)

    return title
```

- [ ] **Step 2: Update `agent.py` imports** — remove the moved definitions and import them from `compaction.py` instead. The `from .compaction import` line already imports `Summarizer`; extend it and add re-exports so existing `from marim_harness.agent import ...` calls keep working.

Replace the top section of `agent.py` (lines 1–83) with:

```python
from contextlib import AsyncExitStack
from typing import Awaitable, Callable, Optional

from pydantic_ai import Agent, DeferredToolRequests, RunContext
from pydantic_ai.usage import RunUsage

from .agents import (
    agents_index_text,
    discover_agents,
    effective_tools,
    find_agent,
    subagent_instructions,
)
from .compaction import (
    Summarizer,
    Titler,
    clean_title,
    compact_history,
    compact_history_with_summary,
    make_summarizer,
    make_titler,
    render_transcript,
)
from .deps import Deps
from .instructions import load_project_instructions
from .mcp import persist_server_enabled
from .memory import global_scope, load_index, project_scope
from .permissions import Mode, resolve_approvals
from .session import SessionInfo, SessionManager, SessionStore
from .skills import discover_skills, skills_index_text
from .tasks import render_tasks
from .tools.provider import ToolProvider
```

Delete everything from line 30 (`_SUMMARY_INSTRUCTIONS = ...`) through line 83 (`    return title`) in `agent.py` — these are now in `compaction.py`.

- [ ] **Step 3: Run tests**

```bash
.venv/bin/python -m pytest -q
```

Expected: `522 passed`

- [ ] **Step 4: Commit**

```bash
git add src/marim_harness/compaction.py src/marim_harness/agent.py
git commit -m "refactor(compaction): move make_summarizer / make_titler / clean_title from agent.py"
```

---

## Task 2: Extract `McpManager`

**Files:**
- Create: `src/marim_harness/mcp_manager.py`
- Modify: `src/marim_harness/agent.py`

- [ ] **Step 1: Create `src/marim_harness/mcp_manager.py`**

```python
from contextlib import AsyncExitStack
from pathlib import Path
from typing import Optional

from .mcp import persist_server_enabled


class McpManager:
    """Owns MCP server lifecycle: connections, enable/disable, grant resolution."""

    def __init__(self, servers: list, disabled: set[str]) -> None:
        self.mcp_servers: list = list(servers)
        self._live_servers: list = []
        self._mcp_stack: Optional[AsyncExitStack] = None
        self._connected: bool = False
        self.disabled: set[str] = set(disabled)
        self.mcp_status: dict = {"connected": [], "failed": []}

    @staticmethod
    def server_name(server) -> str:
        return str(getattr(server, "id", None) or getattr(server, "tool_prefix", "?"))

    def configured_names(self) -> list[str]:
        return [self.server_name(s) for s in self.mcp_servers]

    def enabled_names(self) -> list[str]:
        return [
            n for s in self._live_servers
            if (n := self.server_name(s)) not in self.disabled
        ]

    def live_toolsets(self) -> list:
        return [
            s for s in self._live_servers
            if self.server_name(s) not in self.disabled
        ]

    def mcp_index_text(self) -> str:
        names = self.enabled_names()
        if not names:
            return ""
        return (
            "MCP servers you can grant to a sub-agent via spawn_agent's `mcp` "
            "argument (e.g. mcp=[" + repr(names[0]) + "]): "
            + ", ".join(names)
        )

    def granted_servers(self, names: list[str] | None) -> tuple[list, list[str]]:
        if not names:
            return [], []
        by_name = {self.server_name(s): s for s in self._live_servers}
        granted: list = []
        unknown: list[str] = []
        for name in dict.fromkeys(names):
            server = by_name.get(name)
            if server is None or name in self.disabled:
                unknown.append(name)
            else:
                granted.append(server)
        return granted, unknown

    def grant_note(self, unknown: list[str]) -> str:
        if not unknown:
            return ""
        bad = ", ".join(f"'{n}'" for n in unknown)
        enabled = self.enabled_names()
        avail = ", ".join(enabled) if enabled else "none"
        return f"(note: ignored unknown MCP server(s) {bad}; enabled: {avail})\n\n"

    async def _connect_one(self, server) -> Optional[str]:
        if self._mcp_stack is None:
            self._mcp_stack = AsyncExitStack()
        try:
            await self._mcp_stack.enter_async_context(server)
        except Exception as exc:
            return str(exc)
        self._live_servers.append(server)
        return None

    async def connect(self) -> dict:
        if self._connected or not self.mcp_servers:
            return self.mcp_status
        self._connected = True
        connected: list[str] = []
        failed: list[tuple[str, str]] = []
        for server in self.mcp_servers:
            name = self.server_name(server)
            if name in self.disabled:
                continue
            err = await self._connect_one(server)
            if err is None:
                connected.append(name)
            else:
                failed.append((name, err))
        self.mcp_status = {"connected": connected, "failed": failed}
        return self.mcp_status

    async def aclose(self) -> None:
        if self._mcp_stack is not None:
            await self._mcp_stack.aclose()
            self._mcp_stack = None
        self._live_servers = []
        self._connected = False

    async def disable_server(self, name: str, workspace_root: Path) -> None:
        self.disabled.add(name)
        persist_server_enabled(workspace_root, name, False)

    async def enable_server(self, name: str, workspace_root: Path) -> Optional[str]:
        self.disabled.discard(name)
        persist_server_enabled(workspace_root, name, True)
        if any(self.server_name(s) == name for s in self._live_servers):
            return None
        server = next(
            (s for s in self.mcp_servers if self.server_name(s) == name), None
        )
        if server is None:
            return f"no such server {name!r}"
        err = await self._connect_one(server)
        if err is None:
            self.mcp_status["connected"].append(name)
            self.mcp_status["failed"] = [
                f for f in self.mcp_status["failed"] if f[0] != name
            ]
        return err
```

- [ ] **Step 2: Update `Harness.__init__` to build `McpManager` and delegate MCP state**

In `agent.py`, at the top of `__init__`, add the import and replace the MCP state block. Replace these lines in `__init__`:

```python
        # OLD — remove these:
        self.mcp_servers: list = list(mcp_servers or [])
        self._live_servers: list = []
        self._mcp_stack: Optional[AsyncExitStack] = None
        self._connected = False
        self.disabled: set[str] = set(mcp_disabled or [])
        self.mcp_status: dict = {"connected": [], "failed": []}
```

with:

```python
        self.mcp = McpManager(mcp_servers or [], set(mcp_disabled or []))
```

Also remove the `from contextlib import AsyncExitStack` import from `agent.py` (no longer used there).

Add the `McpManager` import at the top of `agent.py`:

```python
from .mcp_manager import McpManager
```

- [ ] **Step 3: Replace MCP methods on `Harness` with delegates**

Remove `_server_name`, `_connect_one`, `connect`, `aclose`, `disable_server`, `enable_server`, `_granted_servers`, `_enabled_server_names`, `mcp_index_text`, `_mcp_grant_note`, `configured_names` from `Harness` and replace with:

```python
    # --- MCP delegation ---

    @property
    def _live_servers(self) -> list:
        return self.mcp._live_servers

    @_live_servers.setter
    def _live_servers(self, value: list) -> None:
        self.mcp._live_servers = value

    @property
    def disabled(self) -> set:
        return self.mcp.disabled

    @disabled.setter
    def disabled(self, value: set) -> None:
        self.mcp.disabled = value

    @property
    def mcp_status(self) -> dict:
        return self.mcp.mcp_status

    def _server_name(self, server) -> str:
        return McpManager.server_name(server)

    def configured_names(self) -> list[str]:
        return self.mcp.configured_names()

    def _enabled_server_names(self) -> list[str]:
        return self.mcp.enabled_names()

    def mcp_index_text(self) -> str:
        return self.mcp.mcp_index_text()

    def _granted_servers(self, names: list[str] | None) -> tuple[list, list[str]]:
        return self.mcp.granted_servers(names)

    def _mcp_grant_note(self, unknown: list[str]) -> str:
        return self.mcp.grant_note(unknown)

    async def connect(self) -> dict:
        return await self.mcp.connect()

    async def aclose(self) -> None:
        await self.mcp.aclose()

    async def disable_server(self, name: str) -> None:
        await self.mcp.disable_server(name, self.deps.workspace_root)

    async def enable_server(self, name: str) -> Optional[str]:
        return await self.mcp.enable_server(name, self.deps.workspace_root)
```

- [ ] **Step 4: Update `run_turn` to use `mcp.live_toolsets()`**

In `run_turn`, replace the inline toolset filter:

```python
        # OLD:
        toolsets = [
            s for s in self._live_servers
            if self._server_name(s) not in self.disabled
        ]
```

with:

```python
        toolsets = self.mcp.live_toolsets()
```

- [ ] **Step 5: Run tests**

```bash
.venv/bin/python -m pytest -q
```

Expected: `522 passed`

- [ ] **Step 6: Commit**

```bash
git add src/marim_harness/mcp_manager.py src/marim_harness/agent.py
git commit -m "refactor(agent): extract McpManager from Harness"
```

---

## Task 3: Extract `SessionController`

**Files:**
- Create: `src/marim_harness/session_ctrl.py`
- Modify: `src/marim_harness/agent.py`

- [ ] **Step 1: Create `src/marim_harness/session_ctrl.py`**

```python
from typing import Callable, Optional

from pydantic_ai.usage import RunUsage

from .compaction import (
    Summarizer,
    Titler,
    compact_history,
    compact_history_with_summary,
)
from .deps import Deps
from .session import SessionInfo, SessionManager, SessionStore


class SessionController:
    """Owns session lifecycle: history, usage, persistence, compaction, naming."""

    def __init__(
        self,
        store: Optional[SessionStore],
        manager: Optional[SessionManager],
        deps: Deps,
        max_context_tokens: int,
        keep_last_messages: int,
        summarizer: Optional[Summarizer] = None,
        titler: Optional[Titler] = None,
    ) -> None:
        self.store = store
        self.manager = manager
        self.deps = deps
        self.max_context_tokens = max_context_tokens
        self.keep_last_messages = keep_last_messages
        self.summarizer = summarizer
        self.titler = titler
        self.history: list = []
        self.usage: RunUsage = RunUsage()
        self.on_compact: Optional[Callable[[int, int], None]] = None
        self.on_rename: Optional[Callable[[str, str], None]] = None

    @property
    def session_name(self) -> Optional[str]:
        return self.store.name if self.store is not None else None

    def sessions(self) -> list[SessionInfo]:
        if self.manager is None:
            return []
        return self.manager.list()

    def persist(self) -> None:
        if self.store is not None:
            self.store.save(self.history, self.usage, self.deps.tasks.to_payload())

    def set_model(self, model_id: str) -> None:
        if self.store is not None:
            self.store.model = model_id
            self.persist()

    def resume(self) -> int:
        if self.store is None:
            return 0
        self.history, self.usage, tasks = self.store.load()
        self.deps.tasks.load(tasks)
        return len(self.history)

    def reset(self) -> None:
        self.history = []
        self.usage = RunUsage()
        self.deps.tasks.clear()
        if self.store is not None:
            self.store.clear()

    def new_session(self, name: Optional[str] = None) -> None:
        if self.manager is None:
            self.reset()
            return
        self.store = self.manager.create(name)
        self.history = []
        self.usage = RunUsage()
        self.deps.tasks.clear()

    def switch_session(self, session_id: str) -> int:
        if self.manager is None:
            return 0
        self.store = self.manager.store(session_id)
        self.history, self.usage, tasks = self.store.load()
        self.deps.tasks.load(tasks)
        return len(self.history)

    async def maybe_compact(self) -> None:
        before = len(self.history)
        if self.summarizer is not None:
            new_history, did = await compact_history_with_summary(
                self.history, self.max_context_tokens, self.summarizer,
                self.keep_last_messages,
            )
        else:
            new_history, did = compact_history(
                self.history, self.max_context_tokens, self.keep_last_messages,
            )
        if did:
            self.history = new_history
            if self.on_compact is not None:
                self.on_compact(before, len(self.history))

    async def maybe_autoname(self) -> None:
        if (
            self.titler is None
            or self.store is None
            or not self.store.auto_named
            or not self.history
        ):
            return
        old = self.store.name
        try:
            title = await self.titler(self.history)
        except Exception:
            return
        if not title:
            return
        self.store.name = title
        self.store.auto_named = False
        self.persist()
        if self.on_rename is not None:
            self.on_rename(old, title)

    async def rename(self, name: Optional[str] = None) -> Optional[str]:
        if self.store is None:
            return None
        if name:
            new = name.strip()
        elif self.titler is not None and self.history:
            try:
                new = await self.titler(self.history)
            except Exception:
                return None
        else:
            return None
        if not new:
            return None
        self.store.name = new
        self.store.auto_named = False
        self.persist()
        return new
```

- [ ] **Step 2: Update `Harness.__init__` to build `SessionController`**

Add the import to `agent.py`:

```python
from .session_ctrl import SessionController
```

In `Harness.__init__`, replace the session state block:

```python
        # OLD — remove these lines from __init__:
        self.history: list = []
        self.usage = RunUsage()
        self.store = store
        self.manager = manager
        self.max_context_tokens = max_context_tokens
        self.keep_last_messages = keep_last_messages
        self.summarizer = summarizer
        self.titler = titler
        self.on_compact: Optional[Callable[[int, int], None]] = None
        self.on_rename: Optional[Callable[[str, str], None]] = None
```

with:

```python
        self.session = SessionController(
            store, manager, deps,
            max_context_tokens, keep_last_messages,
            summarizer, titler,
        )
```

Also update the `new_session` call inside `__init__` that sets `self.store.model`:

In `Harness.new_session`, replace:

```python
        self.store = self.manager.create(name)
        self.store.model = self.model_id  # keep the current model on the new session
        self.history = []
        self.usage = RunUsage()
        self.deps.tasks.clear()
```

with:

```python
        self.session.new_session(name)
        if self.session.store is not None:
            self.session.store.model = self.model_id
```

- [ ] **Step 3: Replace session methods on `Harness` with delegates**

Remove `resume`, `reset`, `new_session`, `switch_session`, `_persist`, `_maybe_compact`, `_maybe_autoname`, `rename_session`, `sessions`, `session_name` from `Harness` and replace with:

```python
    # --- session delegation ---

    @property
    def history(self) -> list:
        return self.session.history

    @history.setter
    def history(self, value: list) -> None:
        self.session.history = value

    @property
    def usage(self) -> RunUsage:
        return self.session.usage

    @usage.setter
    def usage(self, value: RunUsage) -> None:
        self.session.usage = value

    @property
    def store(self):
        return self.session.store

    @property
    def manager(self):
        return self.session.manager

    @property
    def summarizer(self):
        return self.session.summarizer

    @summarizer.setter
    def summarizer(self, value) -> None:
        self.session.summarizer = value

    @property
    def titler(self):
        return self.session.titler

    @titler.setter
    def titler(self, value) -> None:
        self.session.titler = value

    @property
    def on_compact(self):
        return self.session.on_compact

    @on_compact.setter
    def on_compact(self, value) -> None:
        self.session.on_compact = value

    @property
    def on_rename(self):
        return self.session.on_rename

    @on_rename.setter
    def on_rename(self, value) -> None:
        self.session.on_rename = value

    @property
    def total_tokens(self) -> int:
        return self.session.usage.total_tokens

    @property
    def session_name(self) -> Optional[str]:
        return self.session.session_name

    def sessions(self) -> list[SessionInfo]:
        return self.session.sessions()

    def _persist(self) -> None:
        self.session.persist()

    def resume(self) -> int:
        count = self.session.resume()
        self._apply_saved_model()
        return count

    def reset(self) -> None:
        self.session.reset()

    def new_session(self, name: Optional[str] = None) -> None:
        self.session.new_session(name)
        if self.session.store is not None:
            self.session.store.model = self.model_id

    def switch_session(self, session_id: str) -> int:
        count = self.session.switch_session(session_id)
        self._apply_saved_model()
        return count

    async def rename_session(self, name: Optional[str] = None) -> Optional[str]:
        return await self.session.rename(name)

    async def _maybe_compact(self) -> None:
        await self.session.maybe_compact()

    async def _maybe_autoname(self) -> None:
        await self.session.maybe_autoname()
```

- [ ] **Step 4: Update `run_turn` and `_run_background_subagent` to use session state**

In `run_turn`, replace:

```python
        self.history = result.all_messages()
        self.usage += result.usage
        self._persist()
```

with:

```python
        self.session.history = result.all_messages()
        self.session.usage += result.usage
        self.session.persist()
```

In `_run_subagent`, replace:

```python
        self.usage += result.usage
```

with:

```python
        self.session.usage += result.usage
```

In `_run_background_subagent`, replace:

```python
        self.usage += result.usage
        self._persist()
```

with:

```python
        self.session.usage += result.usage
        self.session.persist()
```

- [ ] **Step 5: Update `set_model` to delegate persistence**

Replace the `set_model` method on `Harness`:

```python
    def set_model(self, model_id: str, *, persist: bool = True) -> None:
        if self.model_source is None:
            return
        model = self.model_source.build(model_id)
        self.current_model = model
        self.model_id = model_id
        self.model_label = self.model_source.label(model_id)
        if self.session.summarizer is not None:
            self.session.summarizer = make_summarizer(model)
        if self.session.titler is not None:
            self.session.titler = make_titler(model)
        if persist:
            self.session.set_model(model_id)
```

- [ ] **Step 6: Run tests**

```bash
.venv/bin/python -m pytest -q
```

Expected: `522 passed`

- [ ] **Step 7: Commit**

```bash
git add src/marim_harness/session_ctrl.py src/marim_harness/agent.py
git commit -m "refactor(agent): extract SessionController from Harness"
```

---

## Task 4: Extract `register_instructions` and slim `Harness.__init__`

**Files:**
- Modify: `src/marim_harness/instructions.py`
- Modify: `src/marim_harness/agent.py`

- [ ] **Step 1: Add `register_instructions` to `instructions.py`**

Append to `src/marim_harness/instructions.py`:

```python
# --- append to src/marim_harness/instructions.py ---

from pydantic_ai import Agent, RunContext

from .agents import agents_index_text, discover_agents
from .deps import Deps
from .memory import global_scope, load_index, project_scope
from .skills import discover_skills, skills_index_text
from .tasks import render_tasks

_PROACTIVE_MEMORY_POLICY = (
    "Proactive memory is ON. Beyond explicit requests, save durable facts that "
    "will help in future sessions with the remember tool: the user's stable "
    "preferences and identity, feedback they give on how you should work, and "
    "project conventions or decisions not derivable from the code or git "
    "history. Convert relative dates to absolute. Do NOT save anything "
    "recoverable from the code, files, or git; one-off conversational details; "
    "or secrets. Prefer updating an existing memory over adding a duplicate."
)

_ON_REQUEST_MEMORY_POLICY = (
    "Save to memory only when the user explicitly asks you to (for example, "
    "\"remember that …\" or the /remember command). Do not save memories "
    "proactively or on your own initiative, even if the user mentions a "
    "preference or fact in passing."
)


def register_instructions(agent: Agent, mcp_manager, proactive_memory: bool) -> None:
    """Register all dynamic instruction closures on ``agent``."""

    @agent.instructions
    def _project_instructions(ctx: RunContext[Deps]) -> str:
        text = load_project_instructions(ctx.deps.workspace_root)
        if not text:
            return ""
        return f"Project-specific instructions from AGENTS.md:\n\n{text}"

    @agent.instructions
    def _memory_indexes(ctx: RunContext[Deps]) -> str:
        parts = []
        g = load_index(global_scope())
        if g:
            parts.append(f"# User memory (global)\n\n{g}")
        p = load_index(project_scope(ctx.deps.workspace_root))
        if p:
            parts.append(f"# Project memory\n\n{p}")
        if not parts:
            return ""
        return (
            "Persistent memory indexes below. Each line is a one-line hook; "
            "read the full fact with the recall tool (by the entry's title or "
            "slug, with the matching scope). Save new durable facts with the "
            "remember tool.\n\n" + "\n\n".join(parts)
        )

    @agent.instructions
    def _skill_index(ctx: RunContext[Deps]) -> str:
        text = skills_index_text(discover_skills(ctx.deps.workspace_root))
        if not text:
            return ""
        return (
            "Available skills below — each is a packaged workflow. When a "
            "task matches one's description, load its full instructions with "
            "the activate_skill tool (by name) and follow them.\n\n" + text
        )

    @agent.instructions
    def _agent_index(ctx: RunContext[Deps]) -> str:
        text = agents_index_text(discover_agents(ctx.deps.workspace_root))
        return (
            "Sub-agents you can delegate to with the spawn_agent tool (each "
            "runs in isolation and reports back; spawn several in one turn to "
            "fan out independent work):\n\n" + text
        )

    @agent.instructions
    def _mcp_index(ctx: RunContext[Deps]) -> str:
        return mcp_manager.mcp_index_text()

    @agent.instructions
    def _task_state(ctx: RunContext[Deps]) -> str:
        items = ctx.deps.tasks.items
        if not items:
            return ""
        return (
            "Your current task checklist (✔ done · ▸ in progress · ○ "
            "pending):\n\n" + render_tasks(items) + "\n\nKeep it current with "
            "the update_tasks tool: pass the full list, keep one item in "
            "progress, and mark items done as you complete them."
        )

    @agent.instructions
    def _memory_policy(ctx: RunContext[Deps]) -> str:
        if proactive_memory:
            return _PROACTIVE_MEMORY_POLICY
        return _ON_REQUEST_MEMORY_POLICY
```

- [ ] **Step 2: Update `Harness.__init__` to call `register_instructions`**

Update the `from .instructions import` line in `agent.py`:

```python
from .instructions import load_project_instructions, register_instructions
```

In `Harness.__init__`, replace the entire block of 7 `@self.agent.instructions` decorator registrations (currently ~85 lines) with a single call:

```python
        register_instructions(self.agent, self.mcp, proactive_memory)
```

Also remove `self.proactive_memory = proactive_memory` from `__init__` (it was only used by the policy closure, which now closes over the constructor argument directly).

Remove these now-unused imports from `agent.py`:

```python
# Remove from agent.py imports (no longer used directly in agent.py):
from .agents import agents_index_text           # now only in instructions.py
from .instructions import load_project_instructions  # now only in instructions.py
from .memory import global_scope, load_index, project_scope  # now only in instructions.py
from .skills import discover_skills, skills_index_text   # now only in instructions.py
from .tasks import render_tasks                 # now only in instructions.py
```

Note: keep `discover_agents` in the `agent.py` import — it is still used by `_build_subagent` to list available types in the error message.

The remaining imports in `agent.py` should be:

```python
from typing import Optional

from pydantic_ai import Agent, DeferredToolRequests, RunContext
from pydantic_ai.usage import RunUsage

from .agents import discover_agents, effective_tools, find_agent, subagent_instructions
from .compaction import (
    Summarizer,
    Titler,
    make_summarizer,
    make_titler,
)
from .deps import Deps
from .instructions import register_instructions
from .mcp_manager import McpManager
from .permissions import Mode, resolve_approvals
from .session import SessionInfo, SessionManager, SessionStore
from .session_ctrl import SessionController
from .tools.provider import ToolProvider
```

- [ ] **Step 3: Remove now-unused module-level constants from `agent.py`**

Delete `_PROACTIVE_MEMORY_POLICY` and `_ON_REQUEST_MEMORY_POLICY` from `agent.py` — they moved to `instructions.py`.

- [ ] **Step 4: Run tests**

```bash
.venv/bin/python -m pytest -q
```

Expected: `522 passed`

- [ ] **Step 5: Run linter**

```bash
.venv/bin/python -m ruff check src/marim_harness/agent.py src/marim_harness/instructions.py
```

Expected: no errors. Fix any unused-import warnings that appear.

- [ ] **Step 6: Commit**

```bash
git add src/marim_harness/instructions.py src/marim_harness/agent.py
git commit -m "refactor(agent): extract register_instructions; slim Harness.__init__"
```
