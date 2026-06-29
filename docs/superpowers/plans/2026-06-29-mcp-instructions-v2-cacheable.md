# MCP Instructions V2 (cacheable) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Deliver the on-discovery MCP instructions from the cacheable message history (injected once at discovery via a custom capability) instead of an uncached dynamic instruction — same text, same behaviour, lower per-turn cost on a caching model.

**Architecture:** A new `AbstractCapability` whose `before_model_request` appends a synthetic `[ModelResponse(marker), ModelRequest(enveloped instructions)]` pair into history for each newly-discovered server, idempotent via a per-server marker scanned from history every call. The V1 dynamic closure is removed.

**Tech Stack:** Python ≥3.10, Pydantic AI 1.107 capabilities API, pytest, uv.

## Global Constraints

- `requires-python = >=3.10` — no 3.11+ only syntax.
- Use `uv` for everything; single-file pytest runs use `--no-cov` (global `fail-under=90`).
- CI order, must pass: `ruff` → `pyright` → `pytest`. pyright gate is **src-only**.
- Ruff line length 100; lint set `E,F,I,UP,B,SIM`.
- Reuse V1's `McpManager.discovered_server_instructions(discovered) -> list[tuple[str,str]]` (already on master, returns sorted non-empty `(server, text)` pairs, 2000-char cap applied by the renderer) — do **not** reimplement it.
- Canonical imports (verified): `from pydantic_ai.capabilities import AbstractCapability`; `from pydantic_ai.tools import AgentDepsT, RunContext`; `from pydantic_ai.models import ModelRequestContext`; `from pydantic_ai.messages import ModelRequest, ModelResponse, TextPart, UserPromptPart`.
- Mirror the existing `ProcessHistory` capability pattern: `@dataclass class X(AbstractCapability[AgentDepsT])` with fields.
- The capability lives in `src/marim_harness/mcp/` and must **not** import `runtime.Deps` (would create an mcp↔runtime import cycle) — stay generic over `AgentDepsT`.
- Commit messages end with the repo trailers:
  ```
  Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>
  Claude-Session: https://claude.ai/code/session_01BszW1YPmM1j3D9nQunTXbH
  ```
- Work on the existing `mcp-instructions-v2-cacheable` branch.

---

### Task 1: The capability + helpers

**Files:**
- Create: `src/marim_harness/mcp/discovered_instructions_capability.py`
- Test: `tests/test_discovered_instructions_capability.py`

**Interfaces:**
- Consumes: `McpManager.discovered_server_instructions` (existing); pydantic-ai message/capability classes.
- Produces: `DiscoveredInstructionsCapability` (dataclass, field `mcp: McpManager`); helpers `_marker`, `_envelope`, `_instruction_messages`, `_injected_servers`.

- [ ] **Step 1: Write the failing tests**

Create `tests/test_discovered_instructions_capability.py`:

```python
import pytest
from pydantic_ai.messages import ModelRequest, UserPromptPart

from marim_harness.mcp.discovered_instructions_capability import (
    DiscoveredInstructionsCapability,
    _injected_servers,
    _instruction_messages,
)


class _FakeMcp:
    """Stand-in for McpManager: returns pairs whose server prefix appears in `discovered`."""
    def __init__(self, pairs):
        self._pairs = pairs

    def discovered_server_instructions(self, discovered):
        return [(s, t) for (s, t) in self._pairs
                if any(d.startswith(s + "_") for d in discovered)]


class _Ctx:
    def __init__(self, discovered):
        self.discovered_tool_names = set(discovered)


class _ReqCtx:
    def __init__(self, messages):
        self.messages = messages


def _base():
    return _ReqCtx([ModelRequest(parts=[UserPromptPart("hi")])])


@pytest.mark.anyio
async def test_no_discovery_is_noop():
    cap = DiscoveredInstructionsCapability(_FakeMcp([("mddocs", "guide")]))
    out = await cap.before_model_request(_Ctx(set()), _base())
    assert len(out.messages) == 1


@pytest.mark.anyio
async def test_injects_one_well_formed_pair():
    cap = DiscoveredInstructionsCapability(_FakeMcp([("mddocs", "Search first.")]))
    out = await cap.before_model_request(_Ctx({"mddocs_search_docs"}), _base())
    assert len(out.messages) == 3
    assert isinstance(out.messages[-1], ModelRequest)            # ends in ModelRequest
    env = out.messages[-1].parts[0].content
    assert "mddocs" in env and "Search first." in env
    kinds = {p.part_kind for m in out.messages for p in m.parts}
    assert kinds <= {"text", "user-prompt"}                      # no tool-call/return parts


@pytest.mark.anyio
async def test_idempotent_when_marker_present():
    cap = DiscoveredInstructionsCapability(_FakeMcp([("mddocs", "Search first.")]))
    out = await cap.before_model_request(_Ctx({"mddocs_x"}), _base())
    n = len(out.messages)
    out2 = await cap.before_model_request(_Ctx({"mddocs_x"}), out)  # marker already in history
    assert len(out2.messages) == n


@pytest.mark.anyio
async def test_self_heals_after_marker_removed():
    cap = DiscoveredInstructionsCapability(_FakeMcp([("mddocs", "Search first.")]))
    out = await cap.before_model_request(_Ctx({"mddocs_x"}), _base())
    out.messages = [ModelRequest(parts=[UserPromptPart("hi")])]     # simulate compaction
    out3 = await cap.before_model_request(_Ctx({"mddocs_x"}), out)
    assert len(out3.messages) == 3                                  # re-injected


@pytest.mark.anyio
async def test_only_uninjected_servers_added():
    cap = DiscoveredInstructionsCapability(_FakeMcp([("mddocs", "g"), ("nasa", "h")]))
    seeded = _ReqCtx(_instruction_messages("mddocs", "g") +
                     [ModelRequest(parts=[UserPromptPart("hi")])])
    out = await cap.before_model_request(_Ctx({"mddocs_x", "nasa_y"}), seeded)
    assert _injected_servers(out.messages) == {"mddocs", "nasa"}    # nasa added, mddocs not duped
    assert sum(1 for m in out.messages
               for p in m.parts if "«mcp-guidance:mddocs»" in getattr(p, "content", "")) == 1


def test_injected_servers_scan():
    msgs = _instruction_messages("mddocs", "g") + _instruction_messages("nasa", "h")
    assert _injected_servers(msgs) == {"mddocs", "nasa"}
```

- [ ] **Step 2: Run to verify they fail**

Run: `uv run pytest tests/test_discovered_instructions_capability.py -q --no-cov`
Expected: FAIL — `ModuleNotFoundError: marim_harness.mcp.discovered_instructions_capability`.

- [ ] **Step 3: Implement the capability**

Create `src/marim_harness/mcp/discovered_instructions_capability.py`:

```python
"""Deliver a discovered MCP server's ``instructions`` from the *cacheable message
history* rather than an uncached dynamic instruction (V1). A custom capability
appends the guidance once, at the moment of discovery, so it lands in the same
request where the model first uses the discovered tools and is a cache-read on
every request thereafter."""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import TYPE_CHECKING

from pydantic_ai.capabilities import AbstractCapability
from pydantic_ai.messages import ModelRequest, ModelResponse, TextPart, UserPromptPart
from pydantic_ai.tools import AgentDepsT, RunContext

if TYPE_CHECKING:
    from pydantic_ai.messages import ModelMessage
    from pydantic_ai.models import ModelRequestContext

    from .manager import McpManager

# A per-server sentinel placed in the synthetic ModelResponse so prior injection is
# detectable by scanning history (guillemets keep it clear of normal prose).
_MARKER_RE = re.compile(r"«mcp-guidance:([^»]+)»")


def _marker(server: str) -> str:
    return f"«mcp-guidance:{server}»"


def _envelope(server: str, text: str) -> str:
    # Labelled so the model reads it as server-authored guidance, not a user utterance.
    return (
        f'[MCP server "{server}" — usage guidance; follow it for that '
        f"server's tools]\n{text}"
    )


def _instruction_messages(server: str, text: str) -> list[ModelMessage]:
    # ModelResponse(marker) + ModelRequest(envelope): the pair ends in a ModelRequest
    # (required: request_context.messages[-1] must be a ModelRequest) and carries no
    # tool-call parts, so it cannot create an unanswered-ToolCallPart resumability hazard.
    return [
        ModelResponse(parts=[TextPart(_marker(server))]),
        ModelRequest(parts=[UserPromptPart(_envelope(server, text))]),
    ]


def _injected_servers(messages: list[ModelMessage]) -> set[str]:
    """Servers already injected this session, found by scanning history for the
    per-server marker. Re-derived every call so it self-heals across resume (fresh
    capability instance) and compaction (marker summarised away → re-inject)."""
    out: set[str] = set()
    for m in messages:
        for p in getattr(m, "parts", []):
            content = getattr(p, "content", None)
            if isinstance(content, str):
                out.update(_MARKER_RE.findall(content))
    return out


@dataclass
class DiscoveredInstructionsCapability(AbstractCapability[AgentDepsT]):
    """Inject discovered MCP servers' instructions into cacheable history, once each.

    Fires after pydantic-ai refreshes ``ctx.discovered_tool_names`` from history, so a
    server discovered this run is injected on the same request the model first uses it."""

    mcp: McpManager

    async def before_model_request(
        self, ctx: RunContext[AgentDepsT], request_context: ModelRequestContext
    ) -> ModelRequestContext:
        discovered = getattr(ctx, "discovered_tool_names", None) or set()
        if not discovered:
            return request_context
        already = _injected_servers(request_context.messages)
        for server, text in self.mcp.discovered_server_instructions(discovered):
            if server not in already:
                request_context.messages.extend(_instruction_messages(server, text))
        return request_context
```

- [ ] **Step 4: Run to verify they pass**

Run: `uv run pytest tests/test_discovered_instructions_capability.py -q --no-cov`
Expected: PASS (7 tests).

- [ ] **Step 5: Lint, type-check, commit**

Run: `uv run ruff check src/marim_harness/mcp/discovered_instructions_capability.py tests/test_discovered_instructions_capability.py && uv run pyright src/marim_harness/mcp/discovered_instructions_capability.py`
Expected: no errors.

```bash
git add src/marim_harness/mcp/discovered_instructions_capability.py tests/test_discovered_instructions_capability.py
git commit -m "feat: capability to inject discovered MCP instructions into cacheable history"
```

---

### Task 2: Switch over — wire V2, remove V1

**Files:**
- Modify: `src/marim_harness/runtime/harness.py` (reorder `mcp` creation; add capability)
- Modify: `src/marim_harness/runtime/instructions.py` (remove the V1 closure + its import)
- Modify: `src/marim_harness/mcp/catalog.py` (remove now-unused `discovered_instructions_text`)
- Modify: `tests/test_tool_catalog.py` (remove the `discovered_instructions_text` tests)

**Interfaces:**
- Consumes: `DiscoveredInstructionsCapability` (Task 1).
- Produces: V2 delivery wired into `build_collaborators`; V1 closure gone.

- [ ] **Step 1: Reorder `mcp` creation and add the capability (harness.py)**

In `src/marim_harness/runtime/harness.py`, the current order is:
```python
    agent = Agent(
        ...
        capabilities=[
            ProcessHistory(_drop_nameless_tool_calls),
            ProcessHistory(suggest_unknown_tool_retry),
        ],
    )
    provider.register(agent)
    mcp = McpManager(cfg.mcp_servers or [], set(cfg.mcp_disabled or []))
    register_instructions(agent, mcp, cfg.proactive_memory)
```

Change to (move `mcp = McpManager(...)` *above* `agent = Agent(`, and add the capability — `McpManager` only needs `cfg`, so the move is safe):
```python
    mcp = McpManager(cfg.mcp_servers or [], set(cfg.mcp_disabled or []))
    agent = Agent(
        ...
        capabilities=[
            ProcessHistory(_drop_nameless_tool_calls),
            ProcessHistory(suggest_unknown_tool_retry),
            DiscoveredInstructionsCapability(mcp),
        ],
    )
    provider.register(agent)
    register_instructions(agent, mcp, cfg.proactive_memory)
```
(Keep the existing explanatory comment block above `capabilities=`; append one line noting `DiscoveredInstructionsCapability` injects discovered servers' instructions into cacheable history.)

Add the import near the other capability import (`from pydantic_ai.capabilities import ProcessHistory`):
```python
from ..mcp.discovered_instructions_capability import DiscoveredInstructionsCapability
```

- [ ] **Step 2: Remove the V1 closure (instructions.py)**

In `src/marim_harness/runtime/instructions.py`, delete the closure:
```python
    @agent.instructions
    def _discovered_instructions(ctx: RunContext[Deps]) -> str:
        discovered = getattr(ctx, "discovered_tool_names", None) or set()
        return discovered_instructions_text(mcp_manager, discovered)
```
and change the import `from ..mcp.catalog import discovered_instructions_text, tool_catalog_text` back to `from ..mcp.catalog import tool_catalog_text`.

- [ ] **Step 3: Remove the now-unused `discovered_instructions_text` (catalog.py)**

In `src/marim_harness/mcp/catalog.py`, delete the `discovered_instructions_text(mcp, discovered)` function (its only caller was the closure just removed). **Keep** `render_discovered_instructions` and `_INSTRUCTIONS_CAP` — `discovered_server_instructions`/the renderer are still the source of the text.

- [ ] **Step 4: Remove the `discovered_instructions_text` tests (test_tool_catalog.py)**

In `tests/test_tool_catalog.py`, delete the two tests that import/exercise `discovered_instructions_text` (`test_discovered_instructions_text_empty_when_nothing_discovered`, `test_discovered_instructions_text_renders_when_discovered`) and the `_FakeMcpInstr` helper and the `from marim_harness.mcp.catalog import discovered_instructions_text` import. **Keep** all `render_discovered_instructions` tests.

- [ ] **Step 5: Run gates**

Run: `uv run ruff check src tests && uv run pyright && uv run pytest tests/test_tool_catalog.py tests/test_discovered_instructions_capability.py tests/test_mcp_tool_search.py -q --no-cov`
Expected: ruff clean; pyright 0 errors (no dangling `discovered_instructions_text` references); listed tests pass.

Also grep to confirm no stragglers:
Run: `grep -rn "discovered_instructions_text\|_discovered_instructions" src tests`
Expected: no matches.

- [ ] **Step 6: Commit**

```bash
git add src/marim_harness/runtime/harness.py src/marim_harness/runtime/instructions.py src/marim_harness/mcp/catalog.py tests/test_tool_catalog.py
git commit -m "feat: deliver on-discovery MCP instructions via cacheable history (remove V1 closure)"
```

---

### Task 3: Live V1-vs-V2 measurement (controller-run — success criterion)

Confirm V2 (a) preserves behaviour (the model still follows the injected instructions) and (b) actually moves the instructions into the cache on a caching model. Run by the controller; no commit.

- [ ] **Step 1: Behaviour parity — re-run the canary on this branch**

Use the canary harness from scratchpad (`canary_server.py`) — a local MCP server whose `instructions` mandate ending replies with `WIDGET-REGION: ZORP-7`. Run sonnet on this branch, tool search on, the widget task, in a fresh temp workspace. Confirm the model still discovers the tool and emits `ZORP-7` (the instructions still reach it via history). Expected: obeyed, same as V1. If V2 breaks behaviour (token absent despite discovery), STOP and report.

- [ ] **Step 2: Cache effect — V1 (master) vs V2 (this branch) on sonnet**

For each arm, run sonnet (`MARIM_MODEL=anthropic/claude-sonnet-4-6`, `MARIM_TOOL_SEARCH=on`) on a **multi-turn** MCP task that discovers mddocs and then makes several further mddocs calls (so the cache warms and there are multiple post-discovery requests). From each run's session, for the requests **after** discovery, compute the uncached input per request as `input_tokens − cache_read_tokens`. Compare arms:
- V2 expectation: after the discovery request, the per-request uncached input is **lower** than V1 by roughly the instructions size (~2k), because the instructions are now in the cache-read region instead of the uncached dynamic block.

Expected: V2's post-discovery uncached-input/request is measurably below V1's. If there is no reduction, V2 delivered no benefit — report that honestly (the feature would then be redundant).

- [ ] **Step 3: Record results** in the progress ledger and report; update the mddocs doc `meh4oj9p` "Does V2 help?" section with the measured V1-vs-V2 numbers (replacing the predicted reasoning with data).

---

## Self-Review

**Spec coverage:**
- Capability injecting `[ModelResponse(marker), ModelRequest(envelope)]`, idempotent by history scan, self-healing → Task 1. ✅
- Same-turn presence (fires after discovery refresh) → inherent to the capability hook; exercised in Task 3 Step 1. ✅
- Replace V1 (remove closure + `discovered_instructions_text` + its tests; keep renderer/helper) → Task 2. ✅
- Resumability-safe (no tool-call parts) → asserted in Task 1 `test_injects_one_well_formed_pair` (`kinds <= {"text","user-prompt"}`). ✅
- Caching wiring premise → no code change needed (marim already sets OpenRouter cache settings); validated by Task 3 Step 2. ✅
- V1-vs-V2 measurement + behaviour parity → Task 3. ✅

**Placeholder scan:** none — full capability code and test code given; exact import paths; exact harness reorder shown.

**Type consistency:** `DiscoveredInstructionsCapability(mcp: McpManager)`, `before_model_request(ctx, request_context) -> ModelRequestContext`, helpers `_marker/_envelope/_instruction_messages/_injected_servers`, marker regex `«mcp-guidance:([^»]+)»` — consistent across Task 1 code and tests and Task 2 grep.

## Out of scope (YAGNI)

Caching the catalog the same way; any change to instructions text / cap / gating; provider-specific cache-breakpoint tuning.
