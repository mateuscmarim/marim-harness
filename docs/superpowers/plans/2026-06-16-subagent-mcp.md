# Sub-Agent MCP Access Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Let the main agent grant specific MCP servers to a spawned sub-agent at spawn time, none by default, reusing the existing per-server approval hook so gating is unchanged.

**Architecture:** A grant is a filtered sub-list of the Harness's already-connected `_live_servers`, handed to the sub-agent's `Agent.run(..., toolsets=...)`. Because each live server object carries its own `process_tool_call` approval hook bound to `deps`, a granted sub-agent's MCP calls gate exactly like the main agent's — no new permission code. The main agent names servers via a new `mcp: list[str]` argument on the `spawn_agent` tool, threaded through the `Deps` runner callbacks into `Harness._run_subagent` / `_run_background_subagent`.

**Tech Stack:** Python 3, Pydantic AI (`Agent`, MCP server toolsets), pytest + anyio, `FunctionModel`/`TestModel` for model stubs.

**Reference:** Design spec at `docs/superpowers/specs/2026-06-16-subagent-mcp-design.md`.

---

## Background the implementer needs

- `Harness` lives in `src/marim_harness/agent.py`. Relevant existing members:
  - `self._live_servers: list` — connected MCP server objects (populated by `connect()`).
  - `self.disabled: set[str]` — names of servers muted at runtime.
  - `Harness._server_name(server) -> str` (static, `agent.py:469`) — a server's display name: `str(getattr(server, "id", None) or getattr(server, "tool_prefix", "?"))`.
  - `Harness._run_subagent(self, type, task, stream_id)` (`agent.py:437`) — builds a sub-agent and `await sub.run(task, deps=self.deps, event_stream_handler=...)`.
  - `Harness._run_background_subagent(self, type, task)` (`agent.py:454`) — `await sub.run(task, deps=self.deps)`.
  - The main turn already filters toolsets the same way (`agent.py:565`): `[s for s in self._live_servers if self._server_name(s) not in self.disabled]`.
  - Instruction closures are registered in `Harness.__init__` with `@self.agent.instructions`; `_agent_index` (`agent.py:169`) lists sub-agent types each turn.
- `Deps` lives in `src/marim_harness/deps.py`. The runner callbacks:
  - `SubAgentRunner = Callable[[str, str, str], Awaitable[str]]` (`deps.py:11`) — `(type, task, tool_call_id) -> report`.
  - `BackgroundAgentRunner = Callable[[str, str], Awaitable[str]]` (`deps.py:18`) — `(type, task) -> report`.
  - Fields `run_subagent` / `run_background_agent` (`deps.py:33`, `deps.py:36`); wired to the Harness methods in `agent.py:209-210`.
- The `spawn_agent` tool is in `src/marim_harness/tools/provider.py:132`. It calls `ctx.deps.run_subagent(type, task, ctx.tool_call_id)` (foreground) or registers `ctx.deps.run_background_agent(type, task)` as a job (background).
- Tests: `tests/test_agent.py`. `_make_harness(model, deps)` (`test_agent.py:36`) builds a `Harness` with `BuiltinToolProvider()`. Existing spawn tests at `test_agent.py:668+` use `TestModel(call_tools=[], custom_output_text="...")` and call `h._run_subagent(...)` directly.
- A fake MCP server for tests is any object exposing `tool_prefix`: `types.SimpleNamespace(tool_prefix="mddocs")` — `_server_name` returns `"mddocs"` (no `id` attr → falls through to `tool_prefix`).

---

## File Structure

- **`src/marim_harness/agent.py`** — owns MCP grant resolution and applies it. New method `_granted_servers`; `_run_subagent` / `_run_background_subagent` gain an `mcp_names` parameter, pass `toolsets=granted`, and prepend the unknown-names note; new instruction closure `_mcp_index` lists enabled servers.
- **`src/marim_harness/deps.py`** — widen the two runner callback type aliases to carry `mcp_names`.
- **`src/marim_harness/tools/provider.py`** — `spawn_agent` gains the `mcp` parameter and forwards it; docstring updated.
- **`tests/test_agent.py`** — unit tests for resolution, both run paths, the note, de-dupe, and the instruction line.
- **`tests/test_provider.py`** — test that `spawn_agent` forwards `mcp` to the runner callbacks.

---

## Task 1: MCP grant resolution (`_granted_servers`)

A pure resolver from requested names to live server objects, plus the list of names it couldn't honor. This is the core logic, testable without running an agent.

**Files:**
- Modify: `src/marim_harness/agent.py` (add method near `_server_name`, after `agent.py:472`)
- Test: `tests/test_agent.py`

- [ ] **Step 1: Write the failing tests**

Add to `tests/test_agent.py`:

```python
def test_granted_servers_resolves_named(tmp_path: Path):
    from types import SimpleNamespace
    from pydantic_ai.models.test import TestModel

    deps = Deps(workspace_root=tmp_path, mode=Mode.auto)
    h = _make_harness(TestModel(), deps)
    a = SimpleNamespace(tool_prefix="mddocs")
    b = SimpleNamespace(tool_prefix="sentry")
    h._live_servers = [a, b]

    granted, unknown = h._granted_servers(["mddocs"])
    assert granted == [a]
    assert unknown == []


def test_granted_servers_none_grants_nothing(tmp_path: Path):
    from types import SimpleNamespace
    from pydantic_ai.models.test import TestModel

    deps = Deps(workspace_root=tmp_path, mode=Mode.auto)
    h = _make_harness(TestModel(), deps)
    h._live_servers = [SimpleNamespace(tool_prefix="mddocs")]

    assert h._granted_servers(None) == ([], [])
    assert h._granted_servers([]) == ([], [])


def test_granted_servers_reports_unknown(tmp_path: Path):
    from types import SimpleNamespace
    from pydantic_ai.models.test import TestModel

    deps = Deps(workspace_root=tmp_path, mode=Mode.auto)
    h = _make_harness(TestModel(), deps)
    h._live_servers = [SimpleNamespace(tool_prefix="mddocs")]

    granted, unknown = h._granted_servers(["mddocs", "nope"])
    assert granted == [h._live_servers[0]]
    assert unknown == ["nope"]


def test_granted_servers_excludes_disabled(tmp_path: Path):
    from types import SimpleNamespace
    from pydantic_ai.models.test import TestModel

    deps = Deps(workspace_root=tmp_path, mode=Mode.auto)
    h = _make_harness(TestModel(), deps)
    h._live_servers = [SimpleNamespace(tool_prefix="mddocs")]
    h.disabled = {"mddocs"}

    granted, unknown = h._granted_servers(["mddocs"])
    assert granted == []
    assert unknown == ["mddocs"]


def test_granted_servers_dedupes(tmp_path: Path):
    from types import SimpleNamespace
    from pydantic_ai.models.test import TestModel

    deps = Deps(workspace_root=tmp_path, mode=Mode.auto)
    h = _make_harness(TestModel(), deps)
    a = SimpleNamespace(tool_prefix="mddocs")
    h._live_servers = [a]

    granted, unknown = h._granted_servers(["mddocs", "mddocs"])
    assert granted == [a]
    assert unknown == []
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `uv run pytest tests/test_agent.py -k granted_servers -v`
Expected: FAIL — `AttributeError: 'Harness' object has no attribute '_granted_servers'`.

- [ ] **Step 3: Implement `_granted_servers`**

In `src/marim_harness/agent.py`, immediately after the `_server_name` static method (after `agent.py:472`), add:

```python
    def _granted_servers(self, names: list[str] | None) -> tuple[list, list[str]]:
        """Resolve requested MCP server names to live server objects for a spawn.

        Returns ``(granted, unknown)``. ``granted`` is the live server objects
        whose name matches a request and is not disabled — passed straight to a
        sub-agent's run as toolsets, so their tools gate via the same approval
        hook as the main agent's. ``unknown`` is requested names with no enabled
        live server (missing or runtime-disabled). Order follows the request;
        duplicate names are honored once."""
        if not names:
            return [], []
        by_name = {self._server_name(s): s for s in self._live_servers}
        granted: list = []
        unknown: list[str] = []
        for name in dict.fromkeys(names):  # de-dupe, preserve first-seen order
            server = by_name.get(name)
            if server is None or name in self.disabled:
                unknown.append(name)
            else:
                granted.append(server)
        return granted, unknown
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `uv run pytest tests/test_agent.py -k granted_servers -v`
Expected: PASS (5 tests).

- [ ] **Step 5: Commit**

```bash
git add src/marim_harness/agent.py tests/test_agent.py
git commit -m "feat(agent): resolve MCP server grants for sub-agents"
```

---

## Task 2: Build the unknown-names note (`_mcp_grant_note`)

A small helper that formats the note prepended to a sub-agent's report when some requested servers couldn't be granted. Pure string logic, so it's tested directly.

**Files:**
- Modify: `src/marim_harness/agent.py` (add method right after `_granted_servers`)
- Test: `tests/test_agent.py`

- [ ] **Step 1: Write the failing tests**

Add to `tests/test_agent.py`:

```python
def test_mcp_grant_note_lists_unknown_and_enabled(tmp_path: Path):
    from types import SimpleNamespace
    from pydantic_ai.models.test import TestModel

    deps = Deps(workspace_root=tmp_path, mode=Mode.auto)
    h = _make_harness(TestModel(), deps)
    h._live_servers = [
        SimpleNamespace(tool_prefix="mddocs"),
        SimpleNamespace(tool_prefix="sentry"),
    ]

    note = h._mcp_grant_note(["nope"])
    assert "nope" in note
    assert "mddocs" in note and "sentry" in note
    assert note.endswith("\n\n")


def test_mcp_grant_note_empty_when_nothing_unknown(tmp_path: Path):
    from pydantic_ai.models.test import TestModel

    deps = Deps(workspace_root=tmp_path, mode=Mode.auto)
    h = _make_harness(TestModel(), deps)
    assert h._mcp_grant_note([]) == ""


def test_mcp_grant_note_handles_no_enabled_servers(tmp_path: Path):
    from pydantic_ai.models.test import TestModel

    deps = Deps(workspace_root=tmp_path, mode=Mode.auto)
    h = _make_harness(TestModel(), deps)
    h._live_servers = []  # nothing enabled

    note = h._mcp_grant_note(["nope"])
    assert "nope" in note
    assert "none" in note.lower()
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `uv run pytest tests/test_agent.py -k mcp_grant_note -v`
Expected: FAIL — `AttributeError: 'Harness' object has no attribute '_mcp_grant_note'`.

- [ ] **Step 3: Implement `_mcp_grant_note` and the enabled-names helper**

In `src/marim_harness/agent.py`, right after `_granted_servers`, add:

```python
    def _enabled_server_names(self) -> list[str]:
        """Live MCP servers currently offered to the model — connected and not
        runtime-disabled. The set a spawn may grant from."""
        return [
            self._server_name(s)
            for s in self._live_servers
            if self._server_name(s) not in self.disabled
        ]

    def _mcp_grant_note(self, unknown: list[str]) -> str:
        """A one-line note for the model when a spawn requested MCP servers that
        couldn't be granted, naming what *is* enabled so it can re-spawn. Empty
        when nothing was unknown. Trailing blank line separates it from the
        sub-agent's report, which it is prepended to."""
        if not unknown:
            return ""
        bad = ", ".join(f"'{n}'" for n in unknown)
        enabled = self._enabled_server_names()
        avail = ", ".join(enabled) if enabled else "none"
        return f"(note: ignored unknown MCP server(s) {bad}; enabled: {avail})\n\n"
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `uv run pytest tests/test_agent.py -k mcp_grant_note -v`
Expected: PASS (3 tests).

- [ ] **Step 5: Commit**

```bash
git add src/marim_harness/agent.py tests/test_agent.py
git commit -m "feat(agent): format unknown-MCP-server note for spawns"
```

---

## Task 3: Grant MCP in the foreground spawn path

Thread `mcp_names` into `_run_subagent`, pass the granted servers as `toolsets`, and prepend the note. Update the `SubAgentRunner` callback type.

**Files:**
- Modify: `src/marim_harness/deps.py:11` (widen `SubAgentRunner`)
- Modify: `src/marim_harness/agent.py:437-452` (`_run_subagent`)
- Test: `tests/test_agent.py`

- [ ] **Step 1: Write the failing tests**

Add to `tests/test_agent.py`. These monkeypatch the built sub-agent so we capture the `toolsets` kwarg without needing a real MCP connection:

```python
def _capture_subagent(h, report="report"):
    """Replace _build_subagent so the spawned agent's run() records the toolsets
    it was given and returns a canned report. Returns the capture dict."""
    from types import SimpleNamespace
    from pydantic_ai.usage import RunUsage

    cap: dict = {}

    class _StubAgent:
        async def run(self, task, **kwargs):
            cap["task"] = task
            cap["toolsets"] = kwargs.get("toolsets")
            return SimpleNamespace(output=report, usage=RunUsage())

    h._build_subagent = lambda type: (_StubAgent(), None)
    return cap


@pytest.mark.anyio
async def test_run_subagent_grants_named_server(tmp_path: Path):
    from types import SimpleNamespace
    from pydantic_ai.models.test import TestModel

    deps = Deps(workspace_root=tmp_path, mode=Mode.auto)
    h = _make_harness(TestModel(), deps)
    server = SimpleNamespace(tool_prefix="mddocs")
    h._live_servers = [server]
    cap = _capture_subagent(h)

    out = await h._run_subagent("explore", "read docs", "sid", ["mddocs"])
    assert out == "report"
    assert cap["toolsets"] == [server]


@pytest.mark.anyio
async def test_run_subagent_default_grants_no_servers(tmp_path: Path):
    from types import SimpleNamespace
    from pydantic_ai.models.test import TestModel

    deps = Deps(workspace_root=tmp_path, mode=Mode.auto)
    h = _make_harness(TestModel(), deps)
    h._live_servers = [SimpleNamespace(tool_prefix="mddocs")]
    cap = _capture_subagent(h)

    await h._run_subagent("explore", "investigate", "sid")
    assert cap["toolsets"] == []


@pytest.mark.anyio
async def test_run_subagent_prepends_unknown_note(tmp_path: Path):
    from pydantic_ai.models.test import TestModel

    deps = Deps(workspace_root=tmp_path, mode=Mode.auto)
    h = _make_harness(TestModel(), deps)
    h._live_servers = []
    _capture_subagent(h, report="FINDINGS")

    out = await h._run_subagent("explore", "investigate", "sid", ["nope"])
    assert "nope" in out
    assert out.rstrip().endswith("FINDINGS")
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `uv run pytest tests/test_agent.py -k "run_subagent_grants or run_subagent_default or run_subagent_prepends" -v`
Expected: FAIL — `_run_subagent()` takes 4 positional args but 5 were given (the `["mddocs"]` arg), and the no-arg call passes `toolsets=None` not `[]`.

- [ ] **Step 3: Update the callback type and `_run_subagent`**

In `src/marim_harness/deps.py`, change line 11 from:

```python
SubAgentRunner = Callable[[str, str, str], Awaitable[str]]
```

to:

```python
# (type, task, tool_call_id, mcp_names) -> the sub-agent's final report.
SubAgentRunner = Callable[[str, str, str, Optional[list[str]]], Awaitable[str]]
```

Confirm `Optional` and `list` are importable in `deps.py` — `from typing import Optional` should already be present (it's used by the field declarations); if `Optional` is not imported, add it to the existing `typing` import. `list` is a builtin generic (Python 3.9+), no import needed.

In `src/marim_harness/agent.py`, replace `_run_subagent` (`agent.py:437-452`) with:

```python
    async def _run_subagent(
        self, type: str, task: str, stream_id: str, mcp_names: list[str] | None = None
    ) -> str:
        """Spawn one isolated sub-agent of ``type``, run it to completion on
        ``task``, and return its final report — streaming its events to the UI
        nested under the spawn. Shares the workspace Deps (read-only use) but
        starts a fresh conversation, so the sub-agent gets a clean context.
        ``mcp_names`` is the MCP servers the main agent granted this spawn (none
        by default); granted servers gate via the same approval hook as the main
        agent's."""
        sub, err = self._build_subagent(type)
        if err is not None:
            return err
        granted, unknown = self._granted_servers(mcp_names)
        result = await sub.run(
            task, deps=self.deps, toolsets=granted,
            event_stream_handler=self._subagent_handler(stream_id),
        )
        # A foreground spawn runs inside the current turn, so its spend is folded
        # into the session total here and persisted by run_turn's _persist.
        self.usage += result.usage
        return self._mcp_grant_note(unknown) + result.output
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `uv run pytest tests/test_agent.py -k "run_subagent" -v`
Expected: PASS — the new tests plus the pre-existing `test_run_subagent_*` (they call with 3 args; `mcp_names` defaults to `None` → `toolsets=[]`, which `Agent.run` accepts).

- [ ] **Step 5: Commit**

```bash
git add src/marim_harness/deps.py src/marim_harness/agent.py tests/test_agent.py
git commit -m "feat(agent): grant MCP servers to foreground sub-agent spawns"
```

---

## Task 4: Grant MCP in the background spawn path

Same treatment for `_run_background_subagent`, and widen the `BackgroundAgentRunner` type.

**Files:**
- Modify: `src/marim_harness/deps.py:18` (widen `BackgroundAgentRunner`)
- Modify: `src/marim_harness/agent.py:454-467` (`_run_background_subagent`)
- Test: `tests/test_agent.py`

- [ ] **Step 1: Write the failing tests**

Add to `tests/test_agent.py` (reuses `_capture_subagent` from Task 3):

```python
@pytest.mark.anyio
async def test_run_background_subagent_grants_named_server(tmp_path: Path):
    from types import SimpleNamespace
    from pydantic_ai.models.test import TestModel

    deps = Deps(workspace_root=tmp_path, mode=Mode.auto)
    h = _make_harness(TestModel(), deps)
    server = SimpleNamespace(tool_prefix="mddocs")
    h._live_servers = [server]
    cap = _capture_subagent(h)

    out = await h._run_background_subagent("general", "do it", ["mddocs"])
    assert out == "report"
    assert cap["toolsets"] == [server]


@pytest.mark.anyio
async def test_run_background_subagent_prepends_unknown_note(tmp_path: Path):
    from pydantic_ai.models.test import TestModel

    deps = Deps(workspace_root=tmp_path, mode=Mode.auto)
    h = _make_harness(TestModel(), deps)
    h._live_servers = []
    _capture_subagent(h, report="DONE")

    out = await h._run_background_subagent("general", "do it", ["nope"])
    assert "nope" in out
    assert out.rstrip().endswith("DONE")
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `uv run pytest tests/test_agent.py -k "run_background_subagent_grants or run_background_subagent_prepends" -v`
Expected: FAIL — `_run_background_subagent()` takes 3 positional args but 4 were given.

- [ ] **Step 3: Update the callback type and `_run_background_subagent`**

In `src/marim_harness/deps.py`, change line 18 from:

```python
BackgroundAgentRunner = Callable[[str, str], Awaitable[str]]
```

to:

```python
BackgroundAgentRunner = Callable[[str, str, Optional[list[str]]], Awaitable[str]]
```

(Keep the existing explanatory comment above it.)

In `src/marim_harness/agent.py`, replace `_run_background_subagent` (`agent.py:454-467`) with:

```python
    async def _run_background_subagent(
        self, type: str, task: str, mcp_names: list[str] | None = None
    ) -> str:
        """Run a sub-agent as a detached background job: same isolation, mode-based
        reach, and MCP grant as a foreground spawn, but with no event streaming —
        the job's result is its final report, surfaced when the agent pulls it.
        Any unknown-server note rides along on that report."""
        sub, err = self._build_subagent(type)
        if err is not None:
            return err
        granted, unknown = self._granted_servers(mcp_names)
        result = await sub.run(task, deps=self.deps, toolsets=granted)
        # A background spawn finishes off-turn, so no run_turn will fold in its
        # spend — count it here and persist right away so the saved session
        # reflects it even if the process exits before the next turn.
        self.usage += result.usage
        self._persist()
        return self._mcp_grant_note(unknown) + result.output
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `uv run pytest tests/test_agent.py -k "run_background_subagent" -v`
Expected: PASS — new tests plus any pre-existing background-spawn tests (they call with 2 args; `mcp_names` defaults to `None`).

- [ ] **Step 5: Commit**

```bash
git add src/marim_harness/deps.py src/marim_harness/agent.py tests/test_agent.py
git commit -m "feat(agent): grant MCP servers to background sub-agent spawns"
```

---

## Task 5: Expose `mcp` on the `spawn_agent` tool

Add the `mcp` parameter to the tool and forward it to both runner callbacks.

**Files:**
- Modify: `src/marim_harness/tools/provider.py:132-157` (`spawn_agent`)
- Test: `tests/test_provider.py`

- [ ] **Step 1: Write the failing tests**

First check how `spawn_agent` is currently tested for a pattern to mirror:

Run: `grep -n "spawn_agent\|run_subagent\|run_background_agent" tests/test_provider.py`

Add to `tests/test_provider.py` (adjust imports to match the file's existing style — it already imports `spawn_agent`'s module or the function; if not, `from marim_harness.tools.provider import spawn_agent`):

```python
@pytest.mark.anyio
async def test_spawn_agent_forwards_mcp_foreground(tmp_path):
    from types import SimpleNamespace
    from marim_harness.deps import Deps
    from marim_harness.permissions import Mode
    from marim_harness.tools.provider import spawn_agent

    calls = {}

    async def fake_runner(type, task, tool_call_id, mcp_names):
        calls["args"] = (type, task, tool_call_id, mcp_names)
        return "ok"

    deps = Deps(workspace_root=tmp_path, mode=Mode.auto)
    deps.run_subagent = fake_runner
    ctx = SimpleNamespace(deps=deps, tool_call_id="tc1")

    out = await spawn_agent(ctx, "explore", "read docs", mcp=["mddocs"])
    assert out == "ok"
    assert calls["args"] == ("explore", "read docs", "tc1", ["mddocs"])


@pytest.mark.anyio
async def test_spawn_agent_forwards_mcp_background(tmp_path):
    from types import SimpleNamespace
    from marim_harness.deps import Deps
    from marim_harness.jobs import JobRegistry
    from marim_harness.permissions import Mode
    from marim_harness.tools.provider import spawn_agent

    captured = {}

    def fake_bg(type, task, mcp_names):
        captured["args"] = (type, task, mcp_names)
        async def _coro():
            return "bg-report"
        return _coro()

    deps = Deps(workspace_root=tmp_path, mode=Mode.auto)
    deps.run_background_agent = fake_bg
    deps.jobs = JobRegistry()
    ctx = SimpleNamespace(deps=deps, tool_call_id="tc2")

    out = await spawn_agent(ctx, "general", "do it", background=True, mcp=["sentry"])
    assert "Started" in out
    assert captured["args"] == ("general", "do it", ["sentry"])


@pytest.mark.anyio
async def test_spawn_agent_default_mcp_is_none(tmp_path):
    from types import SimpleNamespace
    from marim_harness.deps import Deps
    from marim_harness.permissions import Mode
    from marim_harness.tools.provider import spawn_agent

    calls = {}

    async def fake_runner(type, task, tool_call_id, mcp_names):
        calls["mcp_names"] = mcp_names
        return "ok"

    deps = Deps(workspace_root=tmp_path, mode=Mode.auto)
    deps.run_subagent = fake_runner
    ctx = SimpleNamespace(deps=deps, tool_call_id="tc3")

    await spawn_agent(ctx, "explore", "investigate")
    assert calls["mcp_names"] is None
```

Note on `JobRegistry`: confirm the import path and constructor by running `grep -rn "class JobRegistry\|jobs.register" src/marim_harness/jobs.py | head`. If `register` needs different arguments than the existing `spawn_agent` background branch passes, mirror exactly what `provider.py` already does — do not change the registration call, only add `mcp` forwarding. If constructing a real `JobRegistry` in the test is awkward, replace `deps.jobs` with a `SimpleNamespace(register=lambda kind, label, coro: "job-1")` stub and assert on `captured["args"]` only.

- [ ] **Step 2: Run the tests to verify they fail**

Run: `uv run pytest tests/test_provider.py -k spawn_agent_forwards -v`
Expected: FAIL — `spawn_agent()` got an unexpected keyword argument `mcp`.

- [ ] **Step 3: Update `spawn_agent`**

In `src/marim_harness/tools/provider.py`, replace `spawn_agent` (`provider.py:132-157`) with:

```python
async def spawn_agent(
    ctx: RunContext[Deps],
    type: str,
    task: str,
    background: bool = False,
    mcp: list[str] | None = None,
) -> str:
    """Delegate a sub-task to an isolated sub-agent that runs on the same model
    and reports back. `type` is a built-in — `explore` (read-only investigation;
    reports findings, changes nothing) or `general` (full toolset; carries out a
    focused sub-task autonomously) — or a custom agent by name, as listed in the
    sub-agents index. The sub-agent starts with a clean context, does `task`, and
    its final message becomes this tool's result. Spawn several in one turn to
    fan out independent work; sub-agents cannot spawn further sub-agents.

    Set `background=True` to launch it as a detached job and return immediately
    with a job id instead of waiting — keep working, then read its report later
    with job_output / wait_for_job. Background sub-agents don't stream their
    steps; you only see the final report when you pull it.

    `mcp` grants the sub-agent specific MCP servers by name (none by default).
    Pass the names listed as enabled in the sub-agents index — e.g.
    `mcp=["mddocs"]` lets the sub-agent use that server's tools, gated the same
    way your own MCP calls are. Unknown or disabled names are ignored and noted
    in the report."""
    if background:
        if ctx.deps.run_background_agent is None:
            return "Background sub-agents are not available in this context."
        label = f"{type}: {task}"
        job_id = ctx.deps.jobs.register(
            "agent", label, ctx.deps.run_background_agent(type, task, mcp)
        )
        return f"Started {job_id} (agent) — {label[:60]}"
    if ctx.deps.run_subagent is None:
        return "Sub-agents are not available in this context."
    return await ctx.deps.run_subagent(type, task, ctx.tool_call_id, mcp)
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `uv run pytest tests/test_provider.py -k spawn_agent -v`
Expected: PASS — the new forwarding tests plus any pre-existing `spawn_agent` tests.

- [ ] **Step 5: Commit**

```bash
git add src/marim_harness/tools/provider.py tests/test_provider.py
git commit -m "feat(tools): add mcp grant argument to spawn_agent"
```

---

## Task 6: List enabled MCP servers in the spawn instructions

So the model knows which names it can pass to `mcp`. Add an instruction closure that names the currently-enabled servers each turn (silent when none).

**Files:**
- Modify: `src/marim_harness/agent.py` (add `@self.agent.instructions` closure near `_agent_index`, after `agent.py:179`)
- Test: `tests/test_agent.py`

- [ ] **Step 1: Write the failing test**

The closure is a local function inside `__init__`, so test it through behavior: build a Harness, set `_live_servers`, and assert the instruction text the agent would assemble contains the server names. The cleanest seam is a small public method the closure delegates to — add `mcp_index_text` and test that. Add to `tests/test_agent.py`:

```python
def test_mcp_index_text_lists_enabled(tmp_path: Path):
    from types import SimpleNamespace
    from pydantic_ai.models.test import TestModel

    deps = Deps(workspace_root=tmp_path, mode=Mode.auto)
    h = _make_harness(TestModel(), deps)
    h._live_servers = [
        SimpleNamespace(tool_prefix="mddocs"),
        SimpleNamespace(tool_prefix="sentry"),
    ]
    text = h.mcp_index_text()
    assert "mddocs" in text and "sentry" in text
    assert "spawn_agent" in text  # tells the model how to use them


def test_mcp_index_text_silent_when_none(tmp_path: Path):
    from pydantic_ai.models.test import TestModel

    deps = Deps(workspace_root=tmp_path, mode=Mode.auto)
    h = _make_harness(TestModel(), deps)
    h._live_servers = []
    assert h.mcp_index_text() == ""


def test_mcp_index_text_excludes_disabled(tmp_path: Path):
    from types import SimpleNamespace
    from pydantic_ai.models.test import TestModel

    deps = Deps(workspace_root=tmp_path, mode=Mode.auto)
    h = _make_harness(TestModel(), deps)
    h._live_servers = [
        SimpleNamespace(tool_prefix="mddocs"),
        SimpleNamespace(tool_prefix="sentry"),
    ]
    h.disabled = {"sentry"}
    text = h.mcp_index_text()
    assert "mddocs" in text
    assert "sentry" not in text
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `uv run pytest tests/test_agent.py -k mcp_index_text -v`
Expected: FAIL — `AttributeError: 'Harness' object has no attribute 'mcp_index_text'`.

- [ ] **Step 3: Implement `mcp_index_text` and register the instruction closure**

In `src/marim_harness/agent.py`, add a method on `Harness` (place it right after `_enabled_server_names` from Task 2):

```python
    def mcp_index_text(self) -> str:
        """A spawn-time note listing the MCP servers a spawn may grant — the
        enabled live servers. Empty when none are enabled, so the instruction
        stays silent rather than mentioning a feature with nothing behind it."""
        names = self._enabled_server_names()
        if not names:
            return ""
        return (
            "MCP servers you can grant to a sub-agent via spawn_agent's `mcp` "
            "argument (e.g. mcp=[" + repr(names[0]) + "]): "
            + ", ".join(names)
        )
```

Then register a turn-level instruction closure. In `Harness.__init__`, immediately after the `_agent_index` closure (after `agent.py:179`), add:

```python
        @self.agent.instructions
        def _mcp_index(ctx: RunContext[Deps]) -> str:
            """Name the MCP servers a spawn may grant, re-read each turn so a
            server toggled on/off mid-session is reflected. Silent when none are
            enabled."""
            return self.mcp_index_text()
```

(The closure captures `self`; `ctx` is unused but matches the instructions signature the other closures use.)

- [ ] **Step 4: Run the tests to verify they pass**

Run: `uv run pytest tests/test_agent.py -k mcp_index_text -v`
Expected: PASS (3 tests).

- [ ] **Step 5: Commit**

```bash
git add src/marim_harness/agent.py tests/test_agent.py
git commit -m "feat(agent): list grantable MCP servers in spawn instructions"
```

---

## Task 7: Full suite + lint

**Files:** none (verification only)

- [ ] **Step 1: Run the full test suite**

Run: `uv run pytest -q`
Expected: PASS — all tests, including the pre-existing 487 plus the new ones from Tasks 1-6.

- [ ] **Step 2: Lint**

Run: `uv run ruff check src tests`
Expected: no issues. If `list[str] | None` triggers anything or an unused-import lint fires in tests, fix inline.

- [ ] **Step 3: Commit any lint fixes (if needed)**

```bash
git add -A
git commit -m "chore: lint fixes for sub-agent MCP grants"
```

(Skip if there was nothing to fix.)

---

## Self-Review notes (for the implementer)

- **Spec coverage:** Task 1 = `_granted_servers` (mechanism, default-none, disabled-exclusion, de-dupe). Task 2 = unknown-names note. Tasks 3-4 = foreground + background grant via `toolsets`, note prepended. Task 5 = `spawn_agent` `mcp` arg + forwarding. Task 6 = discoverability. Gating-preserved (spec test 7) holds by construction — `_granted_servers` returns the *same* live server objects (asserted by identity in Task 1's `granted == [server]` / `granted == [a]`), and those objects carry the approval hook from `build_mcp_servers`; no copy is made.
- **Type consistency:** `_granted_servers(names) -> (granted, unknown)` used identically in Tasks 3 and 4. `mcp_names` is the parameter name on both run methods and both `Deps` callbacks; `mcp` is the public tool-arg name on `spawn_agent`. `_enabled_server_names` is defined in Task 2 and reused by `_mcp_grant_note` (Task 2) and `mcp_index_text` (Task 6).
- **Ordering caveat:** Tasks must be done in order — Task 2 defines `_enabled_server_names`, which Task 6 reuses; Task 3 defines `_capture_subagent`, which Task 4 reuses. If executing out of order, pull those helpers forward.
