# Tool-search Discovery Catalog Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Stop the model under-searching for deferred MCP tools by injecting a server-grouped catalog of the deferred tool *names* (plus a proactive-search nudge) into the cached prompt prefix — only when tool search is actually deferring.

**Architecture:** A pure renderer builds the catalog text; a manager helper supplies `server -> [tool names]`; a gating helper returns the catalog only when `should_defer` is true; a new async `@agent.instructions` closure (in the existing `register_instructions`, which already holds the MCP manager) injects it. Deterministic output keeps the cached prefix stable.

**Tech Stack:** Python ≥3.10, Pydantic AI (async `@agent.instructions` are supported and awaited), pytest, uv.

## Global Constraints

- `requires-python = >=3.10` — no 3.11+ only syntax.
- Use `uv` for everything; single-file pytest runs use `--no-cov` (the global `fail-under=90` gate fails a partial run otherwise).
- CI order, must pass before a task is "done": `ruff` → `pyright` → `pytest`. pyright gate is **src-only**.
- Ruff line length 100; lint set `E,F,I,UP,B,SIM`. A blind `except Exception` needs `# noqa: BLE001`.
- Per-server cap is a hardcoded constant `_CATALOG_PER_SERVER_CAP = 12` (not an env knob).
- The catalog must be **deterministic** (servers and names sorted) so the cached prefix stays byte-stable across turns.
- The catalog is gated by the **same** `should_defer(policy, count, threshold)` the controller uses for `toolsets_for`, so catalog-shown ⇔ tools-deferred.
- Commit messages end with the repo trailers:
  ```
  Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>
  Claude-Session: https://claude.ai/code/session_01BszW1YPmM1j3D9nQunTXbH
  ```
  (Omitted from per-step examples for brevity — append them.)
- Work on the existing `tool-catalog` git branch (the design doc is committed there).

---

### Task 1: Catalog renderer (pure)

**Files:**
- Create: `src/marim_harness/mcp/catalog.py`
- Test: `tests/test_tool_catalog.py`

**Interfaces:**
- Consumes: nothing.
- Produces: `render_tool_catalog(groups: dict[str, list[str]]) -> str`; `_CATALOG_PER_SERVER_CAP = 12`.

- [ ] **Step 1: Write the failing tests**

Create `tests/test_tool_catalog.py`:

```python
from marim_harness.mcp.catalog import _CATALOG_PER_SERVER_CAP, render_tool_catalog


def test_empty_groups_render_to_empty_string():
    assert render_tool_catalog({}) == ""


def test_renders_servers_sorted_with_names():
    out = render_tool_catalog({"zeta": ["z_b", "z_a"], "alpha": ["a_one"]})
    lines = out.splitlines()
    # preamble first, then servers alphabetically
    assert "search_tools" in lines[0]
    assert lines[1] == "- alpha: a_one"
    # names within a server are rendered in the order given (caller pre-sorts)
    assert lines[2] == "- zeta: z_b, z_a"


def test_per_server_cap_truncates_with_more_suffix():
    names = [f"t{i:02d}" for i in range(_CATALOG_PER_SERVER_CAP + 5)]  # 17 names
    out = render_tool_catalog({"big": names})
    row = [ln for ln in out.splitlines() if ln.startswith("- big:")][0]
    assert "(+5 more)" in row
    assert "t00" in row and "t11" in row  # first 12 shown (t00..t11)
    assert "t12" not in row               # capped


def test_no_more_suffix_when_under_cap():
    out = render_tool_catalog({"small": ["a", "b"]})
    assert "more)" not in out
    assert "- small: a, b" in out
```

- [ ] **Step 2: Run to verify they fail**

Run: `uv run pytest tests/test_tool_catalog.py -q --no-cov`
Expected: FAIL — `ModuleNotFoundError: No module named 'marim_harness.mcp.catalog'`.

- [ ] **Step 3: Implement the renderer**

Create `src/marim_harness/mcp/catalog.py`:

```python
"""The tool-search discovery catalog: a server-grouped list of deferred MCP tool
names injected into the prompt so the model knows what it can discover via
``search_tools`` (the schemas stay deferred). See the tool-catalog design doc."""

# At most this many tool names per server in the catalog; the rest collapse to a
# "(+N more)" hint. Names-only is cheap, but a server with dozens of tools would
# still bloat the prefix — and 12 names is ample query vocabulary for one server.
_CATALOG_PER_SERVER_CAP = 12

_CATALOG_PREAMBLE = (
    "Additional MCP tools are available but not loaded by default. Use the "
    "search_tools function to discover and load them (query with words from the "
    "names below) before concluding a capability is unavailable. Available tools "
    "by server:"
)


def render_tool_catalog(groups: dict[str, list[str]]) -> str:
    """Render a deterministic, server-grouped catalog of deferred tool names. Shows
    at most ``_CATALOG_PER_SERVER_CAP`` names per server, then ``(+N more)``. Servers
    are sorted for byte-stable output (cache-friendly); names are rendered in the
    order given (the caller pre-sorts them). Empty string when there are no groups."""
    if not groups:
        return ""
    lines = [_CATALOG_PREAMBLE]
    for server in sorted(groups):
        names = groups[server]
        shown = names[:_CATALOG_PER_SERVER_CAP]
        extra = len(names) - len(shown)
        suffix = f" (+{extra} more)" if extra > 0 else ""
        lines.append(f"- {server}: {', '.join(shown)}{suffix}")
    return "\n".join(lines)
```

- [ ] **Step 4: Run to verify they pass**

Run: `uv run pytest tests/test_tool_catalog.py -q --no-cov`
Expected: PASS.

- [ ] **Step 5: Lint, type-check, commit**

Run: `uv run ruff check src/marim_harness/mcp/catalog.py tests/test_tool_catalog.py && uv run pyright src/marim_harness/mcp/catalog.py`
Expected: no errors.

```bash
git add src/marim_harness/mcp/catalog.py tests/test_tool_catalog.py
git commit -m "feat: tool-search discovery catalog renderer"
```

---

### Task 2: Manager helper — tools grouped by server

Add a shared private `_tools_per_server`, derive `live_tool_count` from it (DRY), and add `live_tools_by_server` (names per server). The existing tool-search tests pass int "tools" (no `.name`); counting still works because it uses list length, so they stay green.

**Files:**
- Modify: `src/marim_harness/mcp/manager.py` (the `live_tool_count` method, ~lines 79-92)
- Test: `tests/test_mcp_tool_search.py`

**Interfaces:**
- Consumes: existing `live_toolsets()`, `server_name(s)`, `logger`.
- Produces:
  - `async _tools_per_server(self) -> dict[str, list]`
  - `async live_tool_count(self) -> int` (now derived; same behavior)
  - `async live_tools_by_server(self) -> dict[str, list[str]]`

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_mcp_tool_search.py`:

```python
class _NamedTool:
    def __init__(self, name):
        self.name = name


class _NamedServer:
    def __init__(self, sid, names):
        self.id = sid
        self._names = names

    async def list_tools(self):
        return [_NamedTool(n) for n in self._names]


@pytest.mark.anyio
async def test_live_tools_by_server_groups_sorted_names():
    m = _manager_with([
        _NamedServer("mddocs", ["mddocs_b", "mddocs_a"]),
        _NamedServer("nasa", ["nasa_x"]),
    ])
    groups = await m.live_tools_by_server()
    assert groups == {"mddocs": ["mddocs_a", "mddocs_b"], "nasa": ["nasa_x"]}


@pytest.mark.anyio
async def test_live_tools_by_server_best_effort_on_failure():
    class _Bad:
        id = "bad"

        async def list_tools(self):
            raise RuntimeError("boom")

    m = _manager_with([_NamedServer("ok", ["t1"]), _Bad()])
    assert await m.live_tools_by_server() == {"ok": ["t1"]}


@pytest.mark.anyio
async def test_live_tool_count_still_counts_after_refactor():
    # The existing int-tool fakes (no .name) must still count by length.
    m = _manager_with([_FakeServer("a", 4), _FakeServer("b", 6)])
    assert await m.live_tool_count() == 10
```

- [ ] **Step 2: Run to verify they fail**

Run: `uv run pytest tests/test_mcp_tool_search.py -q --no-cov -k "by_server"`
Expected: FAIL — `AttributeError: 'McpManager' object has no attribute 'live_tools_by_server'`.

- [ ] **Step 3: Refactor + add helpers**

In `src/marim_harness/mcp/manager.py`, replace the existing `live_tool_count` method:

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
```

with:

```python
    async def _tools_per_server(self) -> dict[str, list]:
        """Best-effort map of ``server_name -> its raw tool list`` across
        non-disabled live servers. A server with no ``list_tools`` or one that
        raises contributes nothing rather than failing. Shared by
        ``live_tool_count`` and ``live_tools_by_server``."""
        out: dict[str, list] = {}
        for s in self.live_toolsets():
            lister = getattr(s, "list_tools", None)
            if lister is None:
                continue
            try:
                out[self.server_name(s)] = list(await lister())
            except Exception:  # noqa: BLE001 - one server's failure must not sink the rest
                logger.debug("tool listing failed for %s", self.server_name(s), exc_info=True)
        return out

    async def live_tool_count(self) -> int:
        """Best-effort count of tools across non-disabled live MCP servers."""
        return sum(len(v) for v in (await self._tools_per_server()).values())

    async def live_tools_by_server(self) -> dict[str, list[str]]:
        """``server_name -> sorted tool names`` across non-disabled live servers
        (best-effort). Backs the discovery catalog; servers whose tools have no
        usable name are omitted."""
        groups: dict[str, list[str]] = {}
        for name, tools in (await self._tools_per_server()).items():
            tool_names = sorted(
                str(getattr(t, "name", "")) for t in tools if getattr(t, "name", "")
            )
            if tool_names:
                groups[name] = tool_names
        return groups
```

- [ ] **Step 4: Run the tool-search tests (new + existing) to verify green**

Run: `uv run pytest tests/test_mcp_tool_search.py -q --no-cov`
Expected: PASS — the new `by_server` tests AND the pre-existing `live_tool_count` tests (`test_live_tool_count_sums_servers`, `test_live_tool_count_tolerates_failures`, the threshold tests).

- [ ] **Step 5: Lint, type-check, commit**

Run: `uv run ruff check src/marim_harness/mcp/manager.py tests/test_mcp_tool_search.py && uv run pyright src/marim_harness/mcp/manager.py`
Expected: no errors.

```bash
git add src/marim_harness/mcp/manager.py tests/test_mcp_tool_search.py
git commit -m "feat: McpManager.live_tools_by_server (grouped names) + DRY count"
```

---

### Task 3: Gating helper + the `_tool_catalog` instruction

Add the async gating helper to `catalog.py`, then wire a new async `@agent.instructions` closure that injects it. The helper carries the logic and is unit-tested; the closure is a thin wrapper proven by the live verification (Task 4).

**Files:**
- Modify: `src/marim_harness/mcp/catalog.py` (add `tool_catalog_text`)
- Modify: `src/marim_harness/runtime/instructions.py` (`register_instructions`)
- Test: `tests/test_tool_catalog.py`

**Interfaces:**
- Consumes: `should_defer` from `marim_harness.mcp.manager`; `render_tool_catalog` (Task 1); `McpManager.live_tools_by_server` (Task 2); `ctx.deps.workspace.tool_search` / `.tool_search_threshold`.
- Produces: `async tool_catalog_text(mcp, policy: str, threshold: int) -> str`; a registered `_tool_catalog` instruction closure.

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_tool_catalog.py`:

```python
import pytest

from marim_harness.mcp.catalog import tool_catalog_text


class _FakeMcp:
    def __init__(self, groups):
        self._groups = groups

    async def live_tools_by_server(self):
        return self._groups


@pytest.mark.anyio
async def test_catalog_text_shown_when_policy_on():
    mcp = _FakeMcp({"mddocs": ["mddocs_doc_index", "mddocs_grep_docs"]})
    text = await tool_catalog_text(mcp, "on", 15)
    assert "mddocs_doc_index" in text
    assert "search_tools" in text


@pytest.mark.anyio
async def test_catalog_text_empty_when_off():
    mcp = _FakeMcp({"mddocs": ["mddocs_doc_index"]})
    assert await tool_catalog_text(mcp, "off", 15) == ""


@pytest.mark.anyio
async def test_catalog_text_empty_when_auto_below_threshold():
    mcp = _FakeMcp({"mddocs": ["a", "b", "c"]})  # 3 tools
    assert await tool_catalog_text(mcp, "auto", 15) == ""  # 3 <= 15 -> not deferred


@pytest.mark.anyio
async def test_catalog_text_shown_when_auto_above_threshold():
    mcp = _FakeMcp({"s": [f"t{i}" for i in range(20)]})  # 20 tools
    text = await tool_catalog_text(mcp, "auto", 15)  # 20 > 15 -> deferred
    assert text.startswith("Additional MCP tools")
```

Note: `tests/test_tool_catalog.py` needs the `anyio_backend` fixture (asyncio). The repo's `tests/conftest.py` already defines an `anyio_backend` fixture returning `"asyncio"`, available suite-wide — no per-file fixture needed.

- [ ] **Step 2: Run to verify they fail**

Run: `uv run pytest tests/test_tool_catalog.py -q --no-cov -k catalog_text`
Expected: FAIL — `ImportError: cannot import name 'tool_catalog_text'`.

- [ ] **Step 3: Add the gating helper**

In `src/marim_harness/mcp/catalog.py`, add at the bottom:

```python
from .manager import should_defer  # noqa: E402 - kept near use; avoids a top cycle


async def tool_catalog_text(mcp, policy: str, threshold: int) -> str:
    """The catalog block to inject when tool search is deferring this run, else "".
    Gated by the same ``should_defer`` the controller uses for ``toolsets_for``, so
    the catalog is shown exactly when the MCP tools are actually deferred. ``mcp`` is
    an ``McpManager`` (duck-typed: needs ``async live_tools_by_server()``)."""
    groups = await mcp.live_tools_by_server()
    total = sum(len(v) for v in groups.values())
    if not should_defer(policy, total, threshold):
        return ""
    return render_tool_catalog(groups)
```

(If `ruff`/`pyright` object to the late import, move `from .manager import should_defer` to the top of the file instead — `manager.py` does not import `catalog.py`, so there is no cycle either way. Prefer the top import if clean.)

- [ ] **Step 4: Run the helper tests to verify they pass**

Run: `uv run pytest tests/test_tool_catalog.py -q --no-cov`
Expected: PASS (renderer + helper tests).

- [ ] **Step 5: Wire the instruction closure**

In `src/marim_harness/runtime/instructions.py`, add the import near the other imports:

```python
from ..mcp.catalog import tool_catalog_text
```

Inside `register_instructions`, add a new closure alongside the existing ones (e.g. right after `_mcp_index`):

```python
    @agent.instructions
    async def _tool_catalog(ctx: RunContext[Deps]) -> str:
        ws = ctx.deps.workspace
        return await tool_catalog_text(
            mcp_manager, ws.tool_search, ws.tool_search_threshold
        )
```

This is separate from `_mcp_index` (which lists servers for sub-agent grants — a different concern; leave it untouched). Pydantic AI awaits async instruction functions, so the `async def` closure is valid.

- [ ] **Step 6: Full gates**

Run: `uv run ruff check src tests && uv run pyright && uv run pytest tests/test_tool_catalog.py tests/test_mcp_tool_search.py -q --no-cov`
Expected: ruff clean, pyright 0 errors, target tests pass.

- [ ] **Step 7: Commit**

```bash
git add src/marim_harness/mcp/catalog.py src/marim_harness/runtime/instructions.py tests/test_tool_catalog.py
git commit -m "feat: inject the deferred-tool catalog via a gated instruction"
```

---

### Task 4: Live verification (controller-run — the real success criterion)

This is the empirical gate: does the catalog make the model search **unprompted**? It is run by the controller (needs the live OpenRouter model + judgment), not a subagent, and produces no code commit — only a recorded result.

**Steps (controller):**

- [ ] **Step 1: Run owl-alpha headless, UNPROMPTED, tool search ON**

```bash
MARIM_MODEL=openrouter/owl-alpha MARIM_TOOL_SEARCH=on MARIM_DEBUG=1 \
  uv run marim -p "How many document projects do I have? Just give the number." --mode auto \
  > /tmp/catalog-e2e.log 2>&1; echo "exit=$?"
```

The prompt deliberately contains **no** hint to "discover a tool first."

- [ ] **Step 2: Confirm it searched unprompted, then used the real MCP tool**

Inspect the newest session file under `$XDG_DATA_HOME/marim-harness/sessions/marim-harness-*/` (the one matching the run's timestamp) and confirm the ordered parts include:
`search_tools (tool-search)` → `search_tools (tool-search)` → an `mddocs_*` tool call → its return.

Expected: PASS — the model calls `search_tools` on its own (driven by the injected catalog) and then a real mddocs tool. If it does NOT search (e.g. answers "I don't know" or only uses `bash`), that is a real finding — the catalog wording/placement needs another pass; record it and stop for a design revisit rather than declaring success.

- [ ] **Step 3: Record the result in the progress ledger** (no commit).

---

## Self-Review

**Spec coverage:**
- §1 renderer (server-grouped, per-server cap 12, `(+N more)`, deterministic, empty→"") → Task 1. ✅
- §2 `live_tools_by_server` (best-effort, grouped sorted names) + `live_tool_count` DRY refactor → Task 2. ✅
- §3 dynamic async `_tool_catalog` instruction gated by `should_defer`, separate from `_mcp_index` → Task 3. ✅
- §4 consistency (same `should_defer` + count) → Task 3 (`tool_catalog_text` uses `should_defer`). ✅
- Testing incl. the live owl-alpha unprompted verification → Task 4. ✅
- Cap is a hardcoded constant, names-only, no env knob (out-of-scope honored) → Task 1. ✅

**Placeholder scan:** No "TBD"/"handle errors"/"similar to". The one conditional ("if ruff/pyright object to the late import, move it to the top") is a concrete either/or with both branches valid, not deferred work.

**Type consistency:** `render_tool_catalog(dict[str,list[str]])->str`, `_CATALOG_PER_SERVER_CAP=12`, `_tools_per_server()->dict[str,list]`, `live_tool_count()->int`, `live_tools_by_server()->dict[str,list[str]]`, `tool_catalog_text(mcp,policy:str,threshold:int)->str`, `should_defer(str,int,int)->bool`, `ws.tool_search`/`ws.tool_search_threshold` — consistent across tasks and against the real source.

## Out of scope (YAGNI)

Env-configurable cap; per-tool descriptions; server-level summaries; semantic search strategy; rebuilding on `/mcp` toggle beyond the natural per-request recompute.
