# Tool Search Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Defer the variable MCP/plugin tool surface behind Pydantic AI's native tool search (auto-injected `ToolSearch` capability), so MCP schemas aren't loaded into every request — gated by a configurable threshold policy — while keeping the builtin toolset always loaded.

**Architecture:** Pydantic AI's `ToolSearch` capability is auto-injected into every agent and acts on any tool marked `defer_loading=True`. We mark the live MCP toolsets deferred by wrapping them in `DeferredLoadingToolset(CombinedToolset(live))` (both public exports) and passing that via the per-run `toolsets=` argument the controller already varies. A policy (`off`/`auto`/`on` + threshold) decides per-run whether to defer. Builtins stay registered on the Agent (never deferred). No agent rebuild, no private imports, no custom search tool.

**Tech Stack:** Python ≥3.10, Pydantic AI (`pydantic_ai.CombinedToolset`, `pydantic_ai.DeferredLoadingToolset`, auto-injected `ToolSearch`), Textual, pytest, uv.

## Global Constraints

- `requires-python = >=3.10` — no 3.11+ only syntax.
- Use `uv` for everything: `uv run pytest`, `uv run ruff check src tests`, `uv run pyright`.
- CI order, must pass before a task is "done": `ruff` → `pyright` → `pytest`. pyright gate is **src-only** (`[tool.pyright] include = ["src"]`); test-file type noise is out of scope.
- Ruff line length 100; lint set `E,F,I,UP,B,SIM`.
- Pure helpers stay side-effect-free and unit-tested directly; I/O lives in the thin layer.
- `MARIM_TOOL_SEARCH` and `MARIM_TOOL_SEARCH_THRESHOLD` are **not** security-sensitive (a deferred tool is still reachable via search), so they are **NOT** added to `_PROJECT_ENV_BLOCKLIST`. The threshold **is** added to `_POSITIVE_INT_KEYS`.
- Commit messages end with the repo trailers:
  ```
  Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>
  Claude-Session: https://claude.ai/code/session_01BszW1YPmM1j3D9nQunTXbH
  ```
  (Omitted from per-step `git commit` examples for brevity — append them.)
- Work on the existing `tool-search` git branch (already created; the design doc is committed there).
- Default policy is `auto`; default threshold is `15`.

---

### Task 1: Config knobs (`MARIM_TOOL_SEARCH`, `MARIM_TOOL_SEARCH_THRESHOLD`)

Generalize the existing `_mode_env` validator into `_enum_env`, add two `ModelConfig` fields, parse them, and sanitize the threshold env var.

**Files:**
- Modify: `src/marim_harness/config/model.py`
- Modify: `src/marim_harness/config/env.py`
- Test: `tests/test_config.py`

**Interfaces:**
- Consumes: existing `_int_env`, `_VALID_MODES`, `_common_kwargs()`.
- Produces: `_enum_env(name: str, default: str, valid: frozenset[str]) -> str`; `ModelConfig.tool_search: str` (default `"auto"`); `ModelConfig.tool_search_threshold: int` (default `15`).

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_config.py`:

```python
def test_tool_search_defaults_to_auto(monkeypatch):
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-test")
    monkeypatch.delenv("MARIM_TOOL_SEARCH", raising=False)
    cfg = load_config()
    assert cfg.tool_search == "auto"
    assert cfg.tool_search_threshold == 15


def test_tool_search_reads_valid_env(monkeypatch):
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-test")
    monkeypatch.setenv("MARIM_TOOL_SEARCH", "On")
    monkeypatch.setenv("MARIM_TOOL_SEARCH_THRESHOLD", "30")
    cfg = load_config()
    assert cfg.tool_search == "on"
    assert cfg.tool_search_threshold == 30


def test_tool_search_invalid_falls_back_to_auto(monkeypatch):
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-test")
    monkeypatch.setenv("MARIM_TOOL_SEARCH", "sometimes")
    assert load_config().tool_search == "auto"


def test_tool_search_threshold_garbage_falls_back(monkeypatch):
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-test")
    monkeypatch.setenv("MARIM_TOOL_SEARCH_THRESHOLD", "-3")
    # _POSITIVE_INT_KEYS sanitization only runs in load_environment(); _int_env
    # itself returns the default for a non-positive/garbage value at read time.
    assert load_config().tool_search_threshold == 15
```

- [ ] **Step 2: Run to verify they fail**

Run: `uv run pytest tests/test_config.py -q -k tool_search`
Expected: FAIL — `AttributeError: 'ModelConfig' object has no attribute 'tool_search'`.

- [ ] **Step 3: Generalize `_mode_env` into `_enum_env`**

In `src/marim_harness/config/model.py`, replace the `_mode_env` function (currently lines ~214-228) with a generic `_enum_env`, and keep a thin `_mode_env` wrapper so `default_mode` is unchanged:

```python
def _enum_env(name: str, default: str, valid: frozenset[str]) -> str:
    """Read a string env var validated against ``valid`` (case-insensitive). An
    unknown value falls back to ``default`` (warned, not raised)."""
    raw = os.getenv(name)
    if raw is None:
        return default
    value = raw.strip().lower()
    if value not in valid:
        logger.warning(
            "Ignoring invalid %s=%r (expected one of %s); using %r.",
            name, raw, ", ".join(sorted(valid)), default,
        )
        return default
    return value


def _mode_env(name: str, default: str) -> str:
    """Approval-mode env var, validated against ask/auto/plan."""
    return _enum_env(name, default, _VALID_MODES)
```

Add the valid set for tool search near `_VALID_MODES`:

```python
_VALID_TOOL_SEARCH = frozenset({"off", "auto", "on"})
```

- [ ] **Step 4: Add the `ModelConfig` fields**

In `ModelConfig` (after `default_mode: str = "ask"`):

```python
    # Tool search: defer the MCP/plugin tool surface behind Pydantic AI's native
    # tool search. "off" = load all MCP tools every request (today's behavior);
    # "on" = always defer; "auto" = defer only when the live MCP tool count exceeds
    # tool_search_threshold. Builtins are never deferred.
    tool_search: str = "auto"
    tool_search_threshold: int = 15
```

- [ ] **Step 5: Parse them in `_common_kwargs()`**

In the `_common_kwargs()` return dict (after `default_mode=...`):

```python
        tool_search=_enum_env("MARIM_TOOL_SEARCH", "auto", _VALID_TOOL_SEARCH),
        tool_search_threshold=_int_env("MARIM_TOOL_SEARCH_THRESHOLD", 15),
```

- [ ] **Step 6: Sanitize the threshold env var**

In `src/marim_harness/config/env.py`, add to `_POSITIVE_INT_KEYS`:

```python
        "MARIM_TOOL_SEARCH_THRESHOLD",
```

- [ ] **Step 7: Run tests + gates**

Run: `uv run pytest tests/test_config.py -q -k tool_search && uv run ruff check src/marim_harness/config tests/test_config.py && uv run pyright src/marim_harness/config`
Expected: PASS, no errors. Also run the existing default-mode tests to confirm the `_mode_env` wrapper didn't regress: `uv run pytest tests/test_config.py -q -k default_mode` → PASS.

- [ ] **Step 8: Commit**

```bash
git add src/marim_harness/config/model.py src/marim_harness/config/env.py tests/test_config.py
git commit -m "feat: add MARIM_TOOL_SEARCH + threshold config knobs"
```

---

### Task 2: Thread the policy to runtime (`WorkspaceConfig` + bootstrap)

Carry the two knobs onto `WorkspaceConfig` (the controller reads them via `self.deps.workspace`), set from config in `build_harness` — exactly how `command_policy` is threaded today.

**Files:**
- Modify: `src/marim_harness/runtime/deps.py` (`WorkspaceConfig`, ~lines 98-103)
- Modify: `src/marim_harness/runtime/bootstrap.py` (`build_harness`, the `WorkspaceConfig(...)` construction)
- Test: `tests/test_bootstrap.py`

**Interfaces:**
- Consumes: `ModelConfig.tool_search`, `ModelConfig.tool_search_threshold` (Task 1).
- Produces: `WorkspaceConfig.tool_search: str` (default `"auto"`), `WorkspaceConfig.tool_search_threshold: int` (default `15`).

- [ ] **Step 1: Write the failing test**

Append to `tests/test_bootstrap.py`:

```python
def test_build_harness_threads_tool_search_policy(monkeypatch, tmp_path):
    _stub_model_plumbing(monkeypatch)
    _isolate_sessions(monkeypatch, tmp_path)
    monkeypatch.setenv("MARIM_TOOL_SEARCH", "on")
    monkeypatch.setenv("MARIM_TOOL_SEARCH_THRESHOLD", "7")
    harness = bootstrap.build_harness(tmp_path / "ws")
    assert harness.deps.workspace.tool_search == "on"
    assert harness.deps.workspace.tool_search_threshold == 7
```

- [ ] **Step 2: Run to verify it fails**

Run: `uv run pytest tests/test_bootstrap.py -q -k tool_search`
Expected: FAIL — `AttributeError: 'WorkspaceConfig' object has no attribute 'tool_search'`.

- [ ] **Step 3: Add the `WorkspaceConfig` fields**

In `src/marim_harness/runtime/deps.py`, `WorkspaceConfig`:

```python
@dataclass
class WorkspaceConfig:
    """Immutable workspace identity. Set once at construction, never mutated."""

    root: Path
    mode: Mode = Mode.ask
    command_policy: CommandPolicy = field(default_factory=CommandPolicy)
    tool_search: str = "auto"
    tool_search_threshold: int = 15
```

- [ ] **Step 4: Set them in `build_harness`**

In `src/marim_harness/runtime/bootstrap.py`, the `WorkspaceConfig(...)` construction:

```python
        workspace=WorkspaceConfig(
            root=workspace,
            mode=mode,
            command_policy=command_policy,
            tool_search=cfg.tool_search,
            tool_search_threshold=cfg.tool_search_threshold,
        ),
```

- [ ] **Step 5: Run test + gates**

Run: `uv run pytest tests/test_bootstrap.py -q -k tool_search && uv run pyright src/marim_harness/runtime`
Expected: PASS, no errors.

- [ ] **Step 6: Commit**

```bash
git add src/marim_harness/runtime/deps.py src/marim_harness/runtime/bootstrap.py tests/test_bootstrap.py
git commit -m "feat: thread tool-search policy onto WorkspaceConfig"
```

---

### Task 3: MCP manager — policy decision, tool count, deferred composition

Add the pure decision function, an async tool counter, the deferred composition, and a single `toolsets_for(policy, threshold)` entry point the controller calls.

**Files:**
- Modify: `src/marim_harness/mcp/manager.py`
- Test: `tests/test_mcp_tool_search.py` (create)

**Interfaces:**
- Consumes: existing `McpManager.live_toolsets() -> list`, `McpManager.server_name(s)`; `pydantic_ai.CombinedToolset`, `pydantic_ai.DeferredLoadingToolset`.
- Produces:
  - module-level `should_defer(policy: str, count: int, threshold: int) -> bool`
  - `McpManager.live_tool_count() -> int` (async)
  - `McpManager.deferred_toolsets() -> list`
  - `McpManager.toolsets_for(policy: str, threshold: int) -> list` (async)

- [ ] **Step 1: Write the failing tests**

Create `tests/test_mcp_tool_search.py`:

```python
import pytest

from marim_harness.mcp.manager import McpManager, should_defer


@pytest.mark.parametrize(
    ("policy", "count", "threshold", "expected"),
    [
        ("off", 100, 15, False),
        ("on", 0, 15, True),
        ("on", 100, 15, True),
        ("auto", 15, 15, False),   # at threshold -> not deferred (strictly greater)
        ("auto", 16, 15, True),
        ("auto", 3, 15, False),
        ("bogus", 100, 15, False),  # unknown policy is conservative: no deferral
    ],
)
def test_should_defer(policy, count, threshold, expected):
    assert should_defer(policy, count, threshold) is expected


class _FakeServer:
    def __init__(self, name, n_tools):
        self.id = name
        self._n = n_tools

    async def list_tools(self):
        return list(range(self._n))


def _manager_with(servers):
    m = McpManager.__new__(McpManager)  # bypass connect plumbing
    m._live_servers = list(servers)
    m.disabled = set()
    return m


@pytest.mark.anyio
async def test_live_tool_count_sums_servers():
    m = _manager_with([_FakeServer("a", 4), _FakeServer("b", 6)])
    assert await m.live_tool_count() == 10


@pytest.mark.anyio
async def test_live_tool_count_tolerates_failures():
    class _Bad:
        id = "bad"

        async def list_tools(self):
            raise RuntimeError("boom")

    m = _manager_with([_FakeServer("a", 4), _Bad()])
    assert await m.live_tool_count() == 4  # bad server contributes 0, no raise


@pytest.mark.anyio
async def test_toolsets_for_off_returns_live_unwrapped():
    servers = [_FakeServer("a", 50)]
    m = _manager_with(servers)
    result = await m.toolsets_for("off", 15)
    assert result == servers  # unchanged


@pytest.mark.anyio
async def test_toolsets_for_on_wraps_in_deferred():
    from pydantic_ai import DeferredLoadingToolset

    m = _manager_with([_FakeServer("a", 50)])
    result = await m.toolsets_for("on", 15)
    assert len(result) == 1
    assert isinstance(result[0], DeferredLoadingToolset)


@pytest.mark.anyio
async def test_toolsets_for_auto_defers_only_above_threshold():
    from pydantic_ai import DeferredLoadingToolset

    below = _manager_with([_FakeServer("a", 5)])
    assert await below.toolsets_for("auto", 15) == below.live_toolsets()

    above = _manager_with([_FakeServer("a", 50)])
    deferred = await above.toolsets_for("auto", 15)
    assert isinstance(deferred[0], DeferredLoadingToolset)


@pytest.mark.anyio
async def test_deferred_toolsets_empty_when_no_servers():
    m = _manager_with([])
    assert m.deferred_toolsets() == []
    assert await m.toolsets_for("on", 15) == []
```

- [ ] **Step 2: Run to verify it fails**

Run: `uv run pytest tests/test_mcp_tool_search.py -q`
Expected: FAIL — `ImportError: cannot import name 'should_defer'`.

- [ ] **Step 3: Implement the manager additions**

In `src/marim_harness/mcp/manager.py`, add the import near the top (with the other imports):

```python
from pydantic_ai import CombinedToolset, DeferredLoadingToolset
```

Ensure a module logger exists (add if missing, right after `logger = logging.getLogger(__name__)` convention used elsewhere):

```python
logger = logging.getLogger(__name__)
```

Add the pure decision function at module level (above the class):

```python
def should_defer(policy: str, count: int, threshold: int) -> bool:
    """Whether to defer the MCP tool surface behind tool search. ``on`` always
    defers; ``auto`` defers only when ``count`` strictly exceeds ``threshold``;
    anything else (``off`` or an unknown value) never defers."""
    if policy == "on":
        return True
    if policy == "auto":
        return count > threshold
    return False
```

Add these methods to `McpManager`:

```python
    async def live_tool_count(self) -> int:
        """Best-effort count of tools across non-disabled live MCP servers. Uses
        each server's cached ``list_tools()``; a server that can't list contributes
        0 rather than failing the count."""
        total = 0
        for s in self.live_toolsets():
            lister = getattr(s, "list_tools", None)
            if lister is None:
                continue
            try:
                total += len(await lister())
            except Exception:  # noqa: BLE001 - one server's failure must not sink the count
                logger.debug("tool count failed for %s", self.server_name(s), exc_info=True)
        return total

    def deferred_toolsets(self) -> list:
        """The live MCP toolsets combined and marked deferred, so Pydantic AI's
        auto-injected ToolSearch capability hides them until the model searches.
        Empty when there are no live servers."""
        live = self.live_toolsets()
        if not live:
            return []
        return [DeferredLoadingToolset(CombinedToolset(live))]

    async def toolsets_for(self, policy: str, threshold: int) -> list:
        """The toolsets to pass to ``agent.run`` for this turn: the plain live MCP
        toolsets, or — when policy/threshold say so — a single deferred+combined
        toolset behind tool search."""
        if should_defer(policy, await self.live_tool_count(), threshold):
            return self.deferred_toolsets()
        return self.live_toolsets()
```

- [ ] **Step 4: Run tests + gates**

Run: `uv run pytest tests/test_mcp_tool_search.py -q && uv run ruff check src/marim_harness/mcp/manager.py && uv run pyright src/marim_harness/mcp/manager.py`
Expected: PASS, no errors.

- [ ] **Step 5: Commit**

```bash
git add src/marim_harness/mcp/manager.py tests/test_mcp_tool_search.py
git commit -m "feat: MCP manager tool-search policy (count, defer composition, toolsets_for)"
```

---

### Task 4: Controller — apply the policy per run

Replace the unconditional `live_toolsets()` with the policy-aware `toolsets_for(...)`, reading the policy off `self.deps.workspace`.

**Files:**
- Modify: `src/marim_harness/runtime/controller.py` (~line 613, in `run_turn`)
- Test: `tests/test_turn_controller.py`

**Interfaces:**
- Consumes: `McpManager.toolsets_for(policy, threshold)` (Task 3), `WorkspaceConfig.tool_search`/`.tool_search_threshold` (Task 2).
- Produces: no new public surface; behavior change only.

- [ ] **Step 1: Write the failing test**

Append to `tests/test_turn_controller.py` (uses the existing `_make_tc` helper):

```python
@pytest.mark.anyio
async def test_run_turn_defers_mcp_when_policy_on(tmp_path, monkeypatch):
    from pydantic_ai import DeferredLoadingToolset
    from pydantic_ai.messages import ModelResponse, TextPart
    from pydantic_ai.models.function import FunctionModel

    captured = {}

    tc = _make_tc(FunctionModel(lambda m, i: ModelResponse(parts=[TextPart(content="ok")])), tmp_path)
    tc.deps.workspace.tool_search = "on"

    # Stand in two fake MCP servers so deferral has something to wrap.
    class _Srv:
        def __init__(self, name):
            self.id = name

        async def list_tools(self):
            return [1, 2, 3]

    tc.mcp._live_servers = [_Srv("a"), _Srv("b")]
    tc.mcp.disabled = set()

    async def spy(prompt, deferred_results, toolsets, event_stream_handler, resumable):
        captured["toolsets"] = toolsets
        return "ok"

    monkeypatch.setattr(tc, "_run_with_approval", spy)
    await tc.run_turn("hi")
    assert len(captured["toolsets"]) == 1
    assert isinstance(captured["toolsets"][0], DeferredLoadingToolset)


@pytest.mark.anyio
async def test_run_turn_passes_plain_toolsets_when_off(tmp_path, monkeypatch):
    from pydantic_ai.messages import ModelResponse, TextPart
    from pydantic_ai.models.function import FunctionModel

    captured = {}
    tc = _make_tc(FunctionModel(lambda m, i: ModelResponse(parts=[TextPart(content="ok")])), tmp_path)
    tc.deps.workspace.tool_search = "off"

    class _Srv:
        id = "a"

        async def list_tools(self):
            return [1, 2, 3]

    servers = [_Srv()]
    tc.mcp._live_servers = servers
    tc.mcp.disabled = set()

    async def spy(prompt, deferred_results, toolsets, event_stream_handler, resumable):
        captured["toolsets"] = toolsets
        return "ok"

    monkeypatch.setattr(tc, "_run_with_approval", spy)
    await tc.run_turn("hi")
    assert captured["toolsets"] == servers  # unwrapped, unchanged
```

- [ ] **Step 2: Run to verify it fails**

Run: `uv run pytest tests/test_turn_controller.py -q -k "defers_mcp or plain_toolsets"`
Expected: FAIL — the spy captures the plain server list even with policy "on" (deferral not wired yet).

- [ ] **Step 3: Wire the policy in `run_turn`**

In `src/marim_harness/runtime/controller.py`, replace:

```python
        toolsets = self.mcp.live_toolsets()
```

with:

```python
        # Tool-search policy: defer the MCP/plugin surface behind Pydantic AI's
        # auto-injected ToolSearch when the policy/threshold call for it, else load
        # the live MCP toolsets as before. Builtins (on the Agent) are unaffected.
        toolsets = await self.mcp.toolsets_for(
            self.deps.workspace.tool_search,
            self.deps.workspace.tool_search_threshold,
        )
```

- [ ] **Step 4: Run tests + gates**

Run: `uv run pytest tests/test_turn_controller.py -q -k "defers_mcp or plain_toolsets" && uv run pyright src/marim_harness/runtime`
Expected: PASS, no errors.

- [ ] **Step 5: Commit**

```bash
git add src/marim_harness/runtime/controller.py tests/test_turn_controller.py
git commit -m "feat: apply tool-search policy when assembling per-run toolsets"
```

---

### Task 5: Config CLI — show + set the two keys

**Files:**
- Modify: `src/marim_harness/interfaces/cli/config.py`
- Test: `tests/test_config_cli.py`

**Interfaces:**
- Consumes: `ModelConfig.tool_search`, `.tool_search_threshold` (Task 1); existing `_ALLOWED_KEYS`, `_ENUM_KEYS`, `_cmd_set`, `_cmd_show`.
- Produces: CLI support for `MARIM_TOOL_SEARCH` (enum-validated) and `MARIM_TOOL_SEARCH_THRESHOLD` (positive-int-validated).

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_config_cli.py`:

```python
def test_set_rejects_invalid_tool_search(monkeypatch, tmp_path):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    err = io.StringIO()
    assert config_cmd.main(["set", "MARIM_TOOL_SEARCH", "maybe"], err=err) == 2
    assert "off" in err.getvalue() and "auto" in err.getvalue() and "on" in err.getvalue()


def test_set_accepts_tool_search(monkeypatch, tmp_path):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    assert config_cmd.main(["set", "MARIM_TOOL_SEARCH", "On"], out=io.StringIO()) == 0
    assert "MARIM_TOOL_SEARCH=on" in (tmp_path / "marim" / ".env").read_text()


def test_set_rejects_non_positive_threshold(monkeypatch, tmp_path):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    err = io.StringIO()
    assert config_cmd.main(["set", "MARIM_TOOL_SEARCH_THRESHOLD", "0"], err=err) == 2
    assert "positive" in err.getvalue().lower()


def test_set_accepts_threshold(monkeypatch, tmp_path):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    assert config_cmd.main(["set", "MARIM_TOOL_SEARCH_THRESHOLD", "25"], out=io.StringIO()) == 0
    assert "MARIM_TOOL_SEARCH_THRESHOLD=25" in (tmp_path / "marim" / ".env").read_text()


def test_show_includes_tool_search(monkeypatch, tmp_path):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    _clear_marim_env(monkeypatch)
    monkeypatch.setenv("MARIM_TOOL_SEARCH", "on")
    out = io.StringIO()
    assert config_cmd.main(["show"], out=out) == 0
    text = out.getvalue()
    assert "tool_search" in text and "on" in text
```

- [ ] **Step 2: Run to verify they fail**

Run: `uv run pytest tests/test_config_cli.py -q -k tool_search`
Expected: FAIL — `MARIM_TOOL_SEARCH` is not in `_ALLOWED_KEYS` (rejected as unknown key).

- [ ] **Step 3: Allow the enum key**

In `src/marim_harness/interfaces/cli/config.py`, add to `_ALLOWED_KEYS`:

```python
    "MARIM_TOOL_SEARCH",
    "MARIM_TOOL_SEARCH_THRESHOLD",
```

Add the enum mapping:

```python
_ENUM_KEYS = {
    "MARIM_DEFAULT_MODE": ("ask", "auto", "plan"),
    "MARIM_TOOL_SEARCH": ("off", "auto", "on"),
}
```

Add a positive-int validation set and check it in `_cmd_set` (right after the existing `_ENUM_KEYS` validation block, before `_persist`):

```python
_POSITIVE_INT_KEYS_CLI = {"MARIM_TOOL_SEARCH_THRESHOLD"}
```

```python
    if key in _POSITIVE_INT_KEYS_CLI:
        try:
            n = int(value)
        except ValueError:
            print(f"error: {key} must be a positive integer", file=err)
            return 2
        if n <= 0:
            print(f"error: {key} must be a positive integer", file=err)
            return 2
        value = str(n)
```

- [ ] **Step 4: Add to `_cmd_show` (text + JSON)**

In the JSON dict:

```python
            "tool_search": cfg.tool_search,
            "tool_search_threshold": cfg.tool_search_threshold,
```

In the text block (after the `default_mode` line):

```python
    print(f"tool_search:         {cfg.tool_search}", file=out)
    print(f"tool_search_thresh:  {cfg.tool_search_threshold}", file=out)
```

- [ ] **Step 5: Run tests + gates**

Run: `uv run pytest tests/test_config_cli.py -q -k tool_search && uv run ruff check src/marim_harness/interfaces/cli/config.py && uv run pyright src/marim_harness/interfaces/cli/config.py`
Expected: PASS, no errors.

- [ ] **Step 6: Commit**

```bash
git add src/marim_harness/interfaces/cli/config.py tests/test_config_cli.py
git commit -m "feat: config CLI show/set for tool-search keys"
```

---

### Task 6: Settings screen — tool-search selector + threshold

Add a tool-search RadioSet (off/auto/on) and a threshold Input to the Config section, saved to `.env`.

**Files:**
- Modify: `src/marim_harness/interfaces/tui/settings.py`
- Test: `tests/test_settings_screen.py`

**Interfaces:**
- Consumes: `env_cfg.tool_search`, `env_cfg.tool_search_threshold` (a `ModelConfig`); existing `_save_env` values dict, `BoxCheckbox`, `RadioSet`/`RadioButton`, `Input`.
- Produces: persists `MARIM_TOOL_SEARCH` + `MARIM_TOOL_SEARCH_THRESHOLD` on Save.

- [ ] **Step 1: Write the failing test**

Append to `tests/test_settings_screen.py`:

```python
@pytest.mark.anyio
async def test_tool_search_selector_saves(isolated_env, monkeypatch, tmp_path):
    from textual.widgets import Input, RadioButton

    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    monkeypatch.delenv("MARIM_TOOL_SEARCH", raising=False)
    app = _Host(_fake_harness(), _env_cfg())  # env_cfg.tool_search == "auto"
    async with app.run_test(size=(120, 40)) as pilot:
        await pilot.pause()
        screen = app.screen
        assert screen.query_one("#toolsearch-auto", RadioButton).value is True
        screen.active_section = "config"
        await pilot.pause()
        screen.query_one("#toolsearch-on", RadioButton).value = True
        screen.query_one("#toolsearch-threshold", Input).value = "20"
        await pilot.pause()
        screen._save_env()
        await pilot.pause()
    assert os.environ.get("MARIM_TOOL_SEARCH") == "on"
    assert os.environ.get("MARIM_TOOL_SEARCH_THRESHOLD") == "20"
```

Note: `_env_cfg()` in this test file returns `ModelConfig(provider="openrouter", model="x")`; with Task 1 done it has `tool_search="auto"`, `tool_search_threshold=15`.

- [ ] **Step 2: Run to verify it fails**

Run: `uv run pytest tests/test_settings_screen.py -q -k tool_search`
Expected: FAIL — no `#toolsearch-auto` widget.

- [ ] **Step 3: Add the widgets to `_config_widgets()`**

In `src/marim_harness/interfaces/tui/settings.py`, add a module constant near `_MODES`:

```python
_TOOL_SEARCH_MODES = ("off", "auto", "on")
```

In `_config_widgets()`, after the default-mode RadioSet block (the `defmode-*` RadioSet), add:

```python
        yield Label("Tool search (MCP/plugin tools)")
        with RadioSet(id="toolsearch-set"):
            for name in _TOOL_SEARCH_MODES:
                yield RadioButton(
                    name,
                    value=(name == self.env_cfg.tool_search),
                    id=f"toolsearch-{name}",
                )
        with Horizontal(classes="frow"):
            yield Label("Tool-search threshold")
            yield Input(
                value=str(self.env_cfg.tool_search_threshold),
                id="toolsearch-threshold",
                type="integer",
            )
```

- [ ] **Step 4: Persist them in `_save_env()`**

In `_save_env`, after the existing `default_mode` read and before/with the `values` dict, read the new widgets and add them. Read the tool-search radio like the default-mode one, and validate the threshold like the context budget:

```python
        ts = self.query_one("#toolsearch-set", RadioSet)
        ts_idx = ts.pressed_index
        tool_search = (
            _TOOL_SEARCH_MODES[ts_idx]
            if 0 <= ts_idx < len(_TOOL_SEARCH_MODES)
            else self.env_cfg.tool_search
        )
        ts_raw = self.query_one("#toolsearch-threshold", Input).value.strip()
        try:
            ts_threshold = int(ts_raw)
        except ValueError:
            status.update("Tool-search threshold must be a positive integer.")
            return
        if ts_threshold <= 0:
            status.update("Tool-search threshold must be a positive integer.")
            return
```

Then add to the `values` dict (alongside `MARIM_DEFAULT_MODE`):

```python
            "MARIM_TOOL_SEARCH": tool_search,
            "MARIM_TOOL_SEARCH_THRESHOLD": str(ts_threshold),
```

(Place the read-and-validate block after the existing context-budget validation so all early-returns happen before any write.)

- [ ] **Step 5: Run test + full settings-screen file**

Run: `uv run pytest tests/test_settings_screen.py -q && uv run ruff check src/marim_harness/interfaces/tui/settings.py && uv run pyright src/marim_harness/interfaces/tui/settings.py`
Expected: PASS, no errors.

- [ ] **Step 6: Commit**

```bash
git add src/marim_harness/interfaces/tui/settings.py tests/test_settings_screen.py
git commit -m "feat: tool-search selector + threshold in the settings screen"
```

---

### Task 7: Resumability — tool-search history parts round-trip

The one integration risk: `ToolSearchCallPart`/`ToolSearchReturnPart` are new message-history parts. Verify marim's session store serializes and restores them losslessly (they're in Pydantic AI's `ModelMessage` union, so the store's type adapter should handle them).

**Files:**
- Test: `tests/test_session_tool_search_roundtrip.py` (create)

**Interfaces:**
- Consumes: `SessionStore` (how `tests/test_persist.py` constructs/uses it — mirror that), `pydantic_ai.messages` parts.

- [ ] **Step 1: Confirm the store's (de)serialization entry points**

Read `tests/test_persist.py` and `src/marim_harness/session/store.py` to see exactly how a `SessionStore` is built in tests and how `save(messages, ...)` / `load()` round-trip a history (the store uses Pydantic AI's `ModelMessagesTypeAdapter`-style dump). Mirror that construction here. (This step is reading only — no code.)

- [ ] **Step 2: Write the round-trip test**

Create `tests/test_session_tool_search_roundtrip.py`. Build a history containing a `ToolSearchCallPart` (in a `ModelResponse`) and its `ToolSearchReturnPart` (in a `ModelRequest`), save it via a real `SessionStore` into `tmp_path`, reload, and assert the parts survive (same `tool_name == "search_tools"`, same `tool_kind == "tool-search"`, content preserved). Construct the store exactly as `tests/test_persist.py` does (use that file's helper/fixture if present):

```python
import pytest
from pydantic_ai.messages import (
    ModelRequest,
    ModelResponse,
    ToolSearchCallPart,
    ToolSearchReturnPart,
)

# Import the store the same way tests/test_persist.py does; adjust to match.
from marim_harness.session.store import SessionStore


def _roundtrip_history(tmp_path, messages):
    # Mirror tests/test_persist.py's SessionStore construction (path, workspace_root,
    # session_id, name). Save then reload; return the restored messages.
    store = SessionStore(
        path=tmp_path / "s.json",
        workspace_root=tmp_path,
        session_id="s",
        name="s",
    )
    store.save(messages, usage=None, tasks=[], duration_seconds=0.0)
    restored, *_ = store.load()
    return restored


@pytest.mark.anyio
async def test_tool_search_parts_round_trip(tmp_path):
    history = [
        ModelResponse(parts=[ToolSearchCallPart(args='{"queries": ["email"]}')]),
        ModelRequest(parts=[
            ToolSearchReturnPart(content={"discovered": ["send_email"]}),
        ]),
    ]
    restored = _roundtrip_history(tmp_path, history)

    parts = [p for m in restored for p in getattr(m, "parts", [])]
    call = [p for p in parts if getattr(p, "tool_name", None) == "search_tools" and p.__class__.__name__.endswith("CallPart")]
    ret = [p for p in parts if getattr(p, "tool_name", None) == "search_tools" and p.__class__.__name__.endswith("ReturnPart")]
    assert call, "ToolSearchCallPart did not survive persistence"
    assert ret, "ToolSearchReturnPart did not survive persistence"
    assert getattr(call[0], "tool_kind", None) == "tool-search"
```

Notes for the implementer:
- The exact `SessionStore(...)` signature and `save`/`load` shapes must be copied from `store.py`/`test_persist.py` — the snippet above is the intent, not verified arg-for-arg. If `ToolSearchCallPart`/`ToolSearchReturnPart` require different constructor kwargs (check `pydantic_ai/messages.py` / `_tool_search.py` — `ToolSearchReturnPart.content` is `kw_only`), adjust to satisfy them.
- If the store does NOT round-trip these parts (e.g. its adapter is pinned to an older union), that is a real finding — STOP and report it (DONE_WITH_CONCERNS), because it means resumability across a tool search is broken and the controller must guard against persisting a dangling search. Do not paper over it.

- [ ] **Step 3: Run the test**

Run: `uv run pytest tests/test_session_tool_search_roundtrip.py -q`
Expected: PASS (parts survive). If it fails on construction signature, fix the test to match `store.py`; if it fails because parts are dropped, report per Step 2's note.

- [ ] **Step 4: Commit**

```bash
git add tests/test_session_tool_search_roundtrip.py
git commit -m "test: tool-search history parts survive session persistence"
```

---

## Self-Review

**Spec coverage:**
- §1 Policy (config: `MARIM_TOOL_SEARCH` off/auto/on default auto, `MARIM_TOOL_SEARCH_THRESHOLD` default 15, invalid→default, threshold in `_POSITIVE_INT_KEYS`) → Task 1. ✅
- §2 Decision point in the turn loop (off/on/auto+threshold) → Tasks 3 (logic) + 4 (wiring). ✅
- §3 Counting (`live_tool_count`) + `deferred_toolsets` composition in the MCP layer → Task 3. ✅
- §4 Config surface (`config show`/`set`) + settings screen → Tasks 5 + 6. ✅
- §5 Resumability across a tool-search round-trip → Task 7. ✅
- §6 builtins never deferred → enforced by only wrapping MCP toolsets (Task 3/4); not a separate task. ✅
- Not-in-blocklist (security note) → Task 1 Global Constraints + env.py change adds only to `_POSITIVE_INT_KEYS`. ✅
- Sub-agents unchanged → no task touches sub-agent tool grants. ✅
- Threading config to runtime (implied by §2 needing the policy at run time) → Task 2. ✅

**Placeholder scan:** No "TBD"/"handle errors"/"similar to". Task 7 contains two explicit "copy the exact signature from store.py / verify constructor kwargs" notes — these are deliberate verification steps with concrete fallbacks (the store's serialization API is the one thing the plan can't pin without reading store.py at implementation time), not deferred work.

**Type consistency:** `should_defer(policy:str,count:int,threshold:int)->bool`, `live_tool_count()->int`, `deferred_toolsets()->list`, `toolsets_for(policy:str,threshold:int)->list`, `WorkspaceConfig.tool_search:str`/`tool_search_threshold:int`, `ModelConfig.tool_search`/`tool_search_threshold`, `_enum_env(name,default,valid)->str`, widget ids `toolsearch-set`/`toolsearch-{off,auto,on}`/`toolsearch-threshold` — consistent across tasks and against the real source read during planning.

## Out of scope (YAGNI)

Deferring builtins; token-size thresholds; sub-agent tool search; forcing provider-native strategies (`bm25`/`regex`) — the auto-injected `ToolSearch` already picks native on Anthropic and falls back to local keywords elsewhere.
