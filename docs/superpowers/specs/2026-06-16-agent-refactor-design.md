# agent.py Refactor Design

**Date:** 2026-06-16  
**Goal:** Decompose the monolithic `Harness` class into clearly bounded subsystems to support future expansion without touching a single 666-line file.

---

## Problem

`agent.py` / `Harness` currently owns seven distinct concerns:

1. Turn execution (`run_turn`, approval loop)
2. Session lifecycle (`resume`, `reset`, `new_session`, `switch_session`, `rename_session`, `_persist`, `_maybe_autoname`)
3. History compaction (`_maybe_compact`)
4. MCP lifecycle (`connect`, `aclose`, `enable_server`, `disable_server`, `_connect_one`, `_granted_servers`, `mcp_index_text`, `_mcp_grant_note`)
5. Sub-agent spawning (`_build_subagent`, `_run_subagent`, `_run_background_subagent`, `_subagent_handler`)
6. Instruction-closure registration (7 closures wired in `__init__`)
7. Summarizer/titler utilities (`make_summarizer`, `make_titler`, `clean_title` — module-level but conceptually belong in `compaction.py`)

Adding a new MCP capability, session feature, or instruction today means navigating the whole file. The goal is to give each concern a clear home.

---

## Approach: Three extracted classes + thin Harness

### File map

| File | Action | Responsibility |
|---|---|---|
| `agent.py` | Shrink (~120 lines) | Turn runner, sub-agent spawning, wiring |
| `mcp_manager.py` | New | MCP lifecycle |
| `session_ctrl.py` | New | Session lifecycle + compaction |
| `instructions.py` | New | 7 instruction-closure registrations |
| `compaction.py` | Extend | + summarizer/titler factories |

No caller changes (`tui/app.py`, `cli/headless.py`). No behavior changes.

---

## Section 1: `McpManager` (`mcp_manager.py`)

Owns all MCP runtime state: configured servers, live connections, the async exit stack, and the disabled set.

```python
class McpManager:
    def __init__(self, servers: list, disabled: set[str]) -> None

    async def connect(self) -> dict
    async def aclose(self) -> None
    async def enable_server(self, name: str, workspace_root: Path) -> Optional[str]
    async def disable_server(self, name: str, workspace_root: Path) -> None

    def configured_names(self) -> list[str]
    def enabled_names(self) -> list[str]
    def live_toolsets(self) -> list
    def mcp_index_text(self) -> str
    def granted_servers(self, names: list[str] | None) -> tuple[list, list[str]]
    def grant_note(self, unknown: list[str]) -> str

    @staticmethod
    def server_name(server) -> str
```

`workspace_root` is passed at call time to `enable_server`/`disable_server` (for config persistence) rather than stored at construction — it is a caller concern, not a manager concern.

---

## Section 2: `SessionController` (`session_ctrl.py`)

Owns history, usage, persistence, compaction, auto-naming, and session switching.

```python
class SessionController:
    def __init__(
        self,
        store: Optional[SessionStore],
        manager: Optional[SessionManager],
        deps: Deps,
        max_context_tokens: int,
        keep_last_messages: int,
        summarizer: Optional[Summarizer] = None,
        titler: Optional[Titler] = None,
    ) -> None
    # state: history, usage
    # callbacks: on_compact, on_rename

    def resume(self) -> int
    def reset(self) -> None
    def new_session(self, name: Optional[str] = None) -> None
    def switch_session(self, session_id: str) -> int
    def sessions(self) -> list[SessionInfo]
    def persist(self) -> None
    def set_model(self, model_id: str) -> None       # persists model choice on store

    async def maybe_compact(self) -> None
    async def maybe_autoname(self) -> None
    async def rename(self, name: Optional[str] = None) -> Optional[str]

    @property
    def session_name(self) -> Optional[str]
    @property
    def history(self) -> list
    @property
    def usage(self) -> RunUsage
```

`deps` is passed at construction so `SessionController` can load/save/clear `deps.tasks` alongside history (current behavior preserved).

---

## Section 3: `instructions.py`

A single module-level function that registers all 7 instruction closures onto the pydantic-ai `Agent`:

```python
def register_instructions(
    agent: Agent,
    mcp_manager: McpManager,
    proactive_memory: bool,
) -> None
```

The closures read `ctx.deps` for workspace-root, tasks, memory/skill/agent discovery — no change to their logic. The MCP index closure calls `mcp_manager.mcp_index_text()` instead of `self.mcp_index_text()`. The memory-policy closure closes over the `proactive_memory` bool.

All 7 closures move verbatim from `Harness.__init__`; no logic changes.

---

## Section 4: `compaction.py` additions

`make_summarizer`, `make_titler`, and `clean_title` move from `agent.py` into `compaction.py`, where the `Summarizer` type alias and `compact_history*` functions already live. No logic changes — pure relocation. `agent.py` imports them from there.

---

## Section 5: Thin `Harness` (`agent.py`)

After extraction, `Harness` owns:

- **Construction**: build `McpManager` + `SessionController`, build pydantic-ai `Agent`, call `register_instructions`, wire `deps.run_subagent` / `deps.run_background_agent`
- **Model switching**: `set_model` — the only piece that touches both `SessionController` (store persistence) and rebuilds `summarizer`/`titler` on the new model
- **Sub-agent spawning**: `_build_subagent`, `_run_subagent`, `_run_background_subagent`, `_subagent_handler` — stay here because they need `current_model` + `provider`
- **Turn runner**: `run_turn`
- **Pass-through properties/methods** for callers: `session_name`, `sessions`, `resume`, `reset`, `new_session`, `switch_session`, `rename_session`, `connect`, `aclose`, `enable_server`, `disable_server`, `configured_names`, `mcp_index_text`, `mcp_status`, `total_tokens`, `history`, `usage`

Pass-throughs are one-liner delegates to `self.session` or `self.mcp`.

---

## Testing

No new test infrastructure needed. Existing `tests/test_agent.py` covers integrated behavior through `Harness` — that surface does not change. The new classes are independently unit-testable (e.g. `McpManager` with fake servers, `SessionController` with a tmp-dir store) but writing those tests is out of scope for this refactor.
