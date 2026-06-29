# MCP Server Instructions on Discovery Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** When the model discovers a deferred MCP server's tools via tool search, automatically surface that server's own `instructions` (capped), and only then — zero per-MCP config.

**Architecture:** A pure renderer builds the guidance block; a manager helper maps discovered tool names to their servers and reads each server's `instructions`; a thin helper composes them; a new `@agent.instructions` closure reads `ctx.discovered_tool_names` and injects the result. Naturally gated — nothing discovered → nothing injected.

**Tech Stack:** Python ≥3.10, Pydantic AI (`RunContext.discovered_tool_names`, `MCPServer.instructions`), pytest, uv.

## Global Constraints

- `requires-python = >=3.10` — no 3.11+ only syntax.
- Use `uv` for everything; single-file pytest runs use `--no-cov` (global `fail-under=90`).
- CI order, must pass before a task is "done": `ruff` → `pyright` → `pytest`. pyright gate is **src-only**.
- Ruff line length 100; lint set `E,F,I,UP,B,SIM`.
- Per-server cap is a hardcoded constant `_INSTRUCTIONS_CAP = 2000` (chars), not an env knob.
- Use `getattr(server, attr, None)` to read server attributes — the live servers are typed `list[object]`, and `MCPServer.instructions` *raises* `AttributeError` before init, which `getattr(..., None)` turns into `None`. This is the file's established pattern (`server_name`, `_tools_per_server`) and keeps src pyright clean.
- Output must be **deterministic** (servers sorted) so the (dynamic) instruction stays byte-stable across turns for a fixed discovered set.
- The whole chain is **sync** — `instructions` is a cached property, no I/O — so the closure is `def`, not `async def`.
- Commit messages end with the repo trailers:
  ```
  Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>
  Claude-Session: https://claude.ai/code/session_01BszW1YPmM1j3D9nQunTXbH
  ```
  (Omitted from per-step examples — append them.)
- Work on the existing `mcp-instructions-on-discovery` branch (the design doc is committed there).

---

### Task 1: Renderer + cap

**Files:**
- Modify: `src/marim_harness/mcp/catalog.py`
- Test: `tests/test_tool_catalog.py`

**Interfaces:**
- Consumes: nothing.
- Produces: `render_discovered_instructions(servers: list[tuple[str, str]]) -> str`; `_INSTRUCTIONS_CAP = 2000`.

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_tool_catalog.py`:

```python
from marim_harness.mcp.catalog import _INSTRUCTIONS_CAP, render_discovered_instructions


def test_discovered_instructions_empty_when_no_servers():
    assert render_discovered_instructions([]) == ""


def test_discovered_instructions_renders_sorted_with_headers():
    out = render_discovered_instructions([("zeta", "Z guide"), ("alpha", "A guide")])
    assert "search_tools" not in out  # this block is usage guidance, not the catalog
    assert out.index("## alpha") < out.index("## zeta")  # sorted by server name
    assert "A guide" in out and "Z guide" in out


def test_discovered_instructions_truncates_over_cap():
    long = "x" * (_INSTRUCTIONS_CAP + 50)
    out = render_discovered_instructions([("big", long)])
    assert "…(truncated)" in out
    # body is clipped to the cap (plus the marker), not the full length
    assert out.count("x") <= _INSTRUCTIONS_CAP


def test_discovered_instructions_no_truncation_under_cap():
    out = render_discovered_instructions([("small", "short guide")])
    assert "truncated" not in out
    assert "## small\nshort guide" in out
```

- [ ] **Step 2: Run to verify they fail**

Run: `uv run pytest tests/test_tool_catalog.py -q --no-cov -k discovered_instructions`
Expected: FAIL — `ImportError: cannot import name 'render_discovered_instructions'`.

- [ ] **Step 3: Implement the renderer**

In `src/marim_harness/mcp/catalog.py`, add (below the existing `render_tool_catalog`):

```python
# At most this many chars of a single server's instructions go into the prompt;
# beyond that we clip with a marker. Server instructions can be long, and this
# block is re-sent each turn after discovery (dynamic instructions aren't cached),
# so the cap bounds the recurring cost — mirrors the catalog's per-server name cap.
_INSTRUCTIONS_CAP = 2000

_DISCOVERED_PREAMBLE = (
    "Usage guidance for the MCP servers you've loaded (follow it for those tools):"
)


def render_discovered_instructions(servers: list[tuple[str, str]]) -> str:
    """Render a deterministic block of server-authored usage instructions for
    servers the model has discovered. ``servers`` is ``(server_name, instructions)``
    pairs (already filtered to non-empty). Each server's text is clipped to
    ``_INSTRUCTIONS_CAP`` chars with a ``…(truncated)`` marker. Servers are sorted
    for byte-stable output. Empty string when there are no servers."""
    if not servers:
        return ""
    lines = [_DISCOVERED_PREAMBLE]
    for name, text in sorted(servers):
        body = text.strip()
        if len(body) > _INSTRUCTIONS_CAP:
            body = body[:_INSTRUCTIONS_CAP].rstrip() + "\n…(truncated)"
        lines.append(f"\n## {name}\n{body}")
    return "\n".join(lines)
```

- [ ] **Step 4: Run to verify they pass**

Run: `uv run pytest tests/test_tool_catalog.py -q --no-cov -k discovered_instructions`
Expected: PASS.

- [ ] **Step 5: Lint, type-check, commit**

Run: `uv run ruff check src/marim_harness/mcp/catalog.py tests/test_tool_catalog.py && uv run pyright src/marim_harness/mcp/catalog.py`
Expected: no errors.

```bash
git add src/marim_harness/mcp/catalog.py tests/test_tool_catalog.py
git commit -m "feat: renderer for discovered-server instructions"
```

---

### Task 2: Manager helper — discovered → server instructions

**Files:**
- Modify: `src/marim_harness/mcp/manager.py`
- Test: `tests/test_mcp_tool_search.py`

**Interfaces:**
- Consumes: existing `live_toolsets()`, `server_name(s)`.
- Produces: `discovered_server_instructions(self, discovered: set[str]) -> list[tuple[str, str]]`.

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_mcp_tool_search.py`:

```python
class _InstrServer:
    """Fake MCP server: tool_prefix + a plain-string instructions attribute."""
    def __init__(self, prefix, instructions):
        self.id = prefix
        self.tool_prefix = prefix
        self.instructions = instructions


class _RaisingInstrServer:
    """Fake whose .instructions raises AttributeError (simulates pre-init)."""
    def __init__(self, prefix):
        self.id = prefix
        self.tool_prefix = prefix

    @property
    def instructions(self):
        raise AttributeError("instructions only available after initialization")


@pytest.mark.anyio
async def test_discovered_server_instructions_selects_by_prefix():
    m = _manager_with([
        _InstrServer("mddocs", "Search first."),
        _InstrServer("nasa", "Unused server."),
    ])
    # only mddocs tools were discovered
    out = m.discovered_server_instructions({"mddocs_doc_index", "mddocs_grep_docs"})
    assert out == [("mddocs", "Search first.")]


@pytest.mark.anyio
async def test_discovered_server_instructions_skips_empty_and_raising():
    m = _manager_with([
        _InstrServer("a", ""),               # empty instructions -> skipped
        _InstrServer("b", None),             # no instructions -> skipped
        _RaisingInstrServer("c"),            # pre-init raise -> getattr None -> skipped
        _InstrServer("d", "Real guide."),    # included
    ])
    out = m.discovered_server_instructions({"a_x", "b_x", "c_x", "d_x"})
    assert out == [("d", "Real guide.")]


@pytest.mark.anyio
async def test_discovered_server_instructions_sorted_and_empty_discovered():
    m = _manager_with([_InstrServer("zoo", "Z"), _InstrServer("ant", "A")])
    assert m.discovered_server_instructions({"zoo_t", "ant_t"}) == [("ant", "A"), ("zoo", "Z")]
    assert m.discovered_server_instructions(set()) == []
```

- [ ] **Step 2: Run to verify they fail**

Run: `uv run pytest tests/test_mcp_tool_search.py -q --no-cov -k discovered_server`
Expected: FAIL — `AttributeError: 'McpManager' object has no attribute 'discovered_server_instructions'`.

- [ ] **Step 3: Implement the helper**

In `src/marim_harness/mcp/manager.py`, add a method to `McpManager` (next to `live_tools_by_server`):

```python
    def discovered_server_instructions(
        self, discovered: set[str]
    ) -> list[tuple[str, str]]:
        """For each non-disabled live server whose tools appear in ``discovered``,
        return ``(server_name, instructions)`` — the server's init-time usage guide.
        Best-effort: a server with no ``tool_prefix``, no/empty instructions, or one
        whose ``.instructions`` raises before init (``getattr`` → ``None``) is
        skipped, so a quiet/half-connected server never breaks a turn. Sorted by
        server name for deterministic output."""
        out: list[tuple[str, str]] = []
        for s in self.live_toolsets():
            prefix = getattr(s, "tool_prefix", None)
            if not prefix or not any(t.startswith(f"{prefix}_") for t in discovered):
                continue
            text = getattr(s, "instructions", None)
            if isinstance(text, str) and text.strip():
                out.append((self.server_name(s), text))
        out.sort(key=lambda pair: pair[0])
        return out
```

- [ ] **Step 4: Run to verify they pass**

Run: `uv run pytest tests/test_mcp_tool_search.py -q --no-cov -k discovered_server`
Expected: PASS.

- [ ] **Step 5: Lint, type-check, commit**

Run: `uv run ruff check src/marim_harness/mcp/manager.py tests/test_mcp_tool_search.py && uv run pyright src/marim_harness/mcp/manager.py`
Expected: no errors.

```bash
git add src/marim_harness/mcp/manager.py tests/test_mcp_tool_search.py
git commit -m "feat: McpManager.discovered_server_instructions"
```

---

### Task 3: Compose helper + wire the instruction

**Files:**
- Modify: `src/marim_harness/mcp/catalog.py` (add `discovered_instructions_text`)
- Modify: `src/marim_harness/runtime/instructions.py` (the `_discovered_instructions` closure)
- Test: `tests/test_tool_catalog.py`

**Interfaces:**
- Consumes: `render_discovered_instructions` (Task 1); `McpManager.discovered_server_instructions` (Task 2); `ctx.discovered_tool_names`.
- Produces: `discovered_instructions_text(mcp, discovered: set[str]) -> str`; a registered `_discovered_instructions` instruction.

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_tool_catalog.py`:

```python
from marim_harness.mcp.catalog import discovered_instructions_text


class _FakeMcpInstr:
    def __init__(self, pairs):
        self._pairs = pairs

    def discovered_server_instructions(self, discovered):
        # ignore filtering here; the manager is unit-tested separately
        return list(self._pairs) if discovered else []


@pytest.mark.anyio
async def test_discovered_instructions_text_empty_when_nothing_discovered():
    mcp = _FakeMcpInstr([("mddocs", "Search first.")])
    assert discovered_instructions_text(mcp, set()) == ""


@pytest.mark.anyio
async def test_discovered_instructions_text_renders_when_discovered():
    mcp = _FakeMcpInstr([("mddocs", "Search first.")])
    text = discovered_instructions_text(mcp, {"mddocs_doc_index"})
    assert "## mddocs" in text and "Search first." in text
```

(The `@pytest.mark.anyio` keeps these consistent with the other async tests in the file; `discovered_instructions_text` is sync, so the marker is harmless — or drop it on these two. The repo's `conftest.py` provides the `anyio_backend` fixture.)

- [ ] **Step 2: Run to verify they fail**

Run: `uv run pytest tests/test_tool_catalog.py -q --no-cov -k discovered_instructions_text`
Expected: FAIL — `ImportError: cannot import name 'discovered_instructions_text'`.

- [ ] **Step 3: Add the compose helper**

In `src/marim_harness/mcp/catalog.py`, add below `render_discovered_instructions`:

```python
def discovered_instructions_text(mcp, discovered: set[str]) -> str:
    """The usage-guidance block to inject for servers the model has discovered this
    run, or "" when nothing has been discovered. ``mcp`` is an ``McpManager``
    (duck-typed: needs ``discovered_server_instructions``)."""
    if not discovered:
        return ""
    return render_discovered_instructions(mcp.discovered_server_instructions(discovered))
```

- [ ] **Step 4: Run the helper tests to verify they pass**

Run: `uv run pytest tests/test_tool_catalog.py -q --no-cov -k discovered_instructions_text`
Expected: PASS.

- [ ] **Step 5: Wire the instruction closure**

In `src/marim_harness/runtime/instructions.py`, extend the existing catalog import (currently `from ..mcp.catalog import tool_catalog_text`) to also import the new helper:

```python
from ..mcp.catalog import discovered_instructions_text, tool_catalog_text
```

Inside `register_instructions`, add a new closure right after `_tool_catalog`:

```python
    @agent.instructions
    def _discovered_instructions(ctx: RunContext[Deps]) -> str:
        discovered = getattr(ctx, "discovered_tool_names", None) or set()
        return discovered_instructions_text(mcp_manager, discovered)
```

This is sync (no I/O — `instructions` is a cached property). It is separate from `_tool_catalog` (which lists deferred tool *names*); this one surfaces the *usage guide* for servers already discovered.

- [ ] **Step 6: Full gates**

Run: `uv run ruff check src tests && uv run pyright && uv run pytest tests/test_tool_catalog.py tests/test_mcp_tool_search.py -q --no-cov`
Expected: ruff clean, pyright 0 errors, target tests pass.

- [ ] **Step 7: Commit**

```bash
git add src/marim_harness/mcp/catalog.py src/marim_harness/runtime/instructions.py tests/test_tool_catalog.py
git commit -m "feat: inject discovered MCP servers' instructions via a gated instruction"
```

---

### Task 4: Live verification (controller-run — success criterion)

Confirm a capable model, on a multi-step MCP task, gets the discovered server's instructions injected *after* it searches — and that a no-discovery run injects nothing. Run by the controller (needs the live model + judgment), no code commit.

**Steps (controller):**

- [ ] **Step 1: Run a real multi-step mddocs task with tool search on, debug logging**

```bash
MARIM_MODEL=anthropic/claude-sonnet-4-6 MARIM_TOOL_SEARCH=on MARIM_DEBUG=1 \
  uv run marim -p "Find my mddocs doc about tool search and tell me its title and one key point. Use the right tools." --mode auto \
  > /tmp/discovery-instr.log 2>&1; echo "exit=$?"
```

- [ ] **Step 2: Confirm the instructions were injected after discovery**

Pick a distinctive phrase from the mddocs server's own instructions (read it first via marim's debug log or the server) and confirm it appears in a model REQUEST that occurs *after* a `search_tools` call — i.e. the guidance was surfaced once the server's tools were discovered. Concretely:
- Confirm the session history shows `search_tools` then an `mddocs_*` tool call (discovery happened).
- Grep the debug log for the `_DISCOVERED_PREAMBLE` text ("Usage guidance for the MCP servers you've loaded") — it must appear, and must NOT appear before the first `search_tools` call.

Expected: PASS — the preamble + mddocs's instructions appear post-discovery. If the preamble never appears despite a successful discovery, that is a real wiring finding — stop and report, don't declare success.

- [ ] **Step 3: Confirm a no-discovery run injects nothing**

Run a trivial prompt that needs no MCP tool (e.g. `-p "What is 2+2?"`). Confirm the `_DISCOVERED_PREAMBLE` does NOT appear in any request (nothing discovered → empty).

- [ ] **Step 4: (Optional) note the per-turn token delta** from the debug/usage output, for the V2 decision recorded in the mddocs design note.

- [ ] **Step 5: Record the result in the progress ledger** (no commit).

---

## Self-Review

**Spec coverage:**
- §1 renderer (sorted, `_INSTRUCTIONS_CAP=2000`, truncation marker, empty→"") → Task 1. ✅
- §2 `discovered_server_instructions` (prefix match, best-effort skip on empty/None/pre-init-raise, sorted) → Task 2. ✅
- §3 `_discovered_instructions` closure reading `ctx.discovered_tool_names`, gated (empty→""), separate from `_tool_catalog` → Task 3. ✅
- §4 cost note → embedded as the `_INSTRUCTIONS_CAP` rationale (Task 1) + Task 4 Step 4. ✅
- Testing incl. live multi-step verification → Task 4. ✅
- Zero-config / no per-server flag / cap hardcoded (out-of-scope honored) → no config task exists. ✅

**Placeholder scan:** No "TBD"/"handle errors"/"similar to". The `getattr(..., None)` best-effort pattern is shown explicitly with its rationale, not hand-waved.

**Type consistency:** `render_discovered_instructions(list[tuple[str,str]])->str`, `_INSTRUCTIONS_CAP=2000`, `discovered_server_instructions(set[str])->list[tuple[str,str]]`, `discovered_instructions_text(mcp, set[str])->str`, `ctx.discovered_tool_names`, tool-prefix match `f"{prefix}_"` — consistent across tasks and against the real source.

## Out of scope (YAGNI)

Per-server opt-in; global `include_instructions`; env-configurable cap; the V2 cacheable append-at-discovery (documented in the mddocs design note + spec as a future optimization).
