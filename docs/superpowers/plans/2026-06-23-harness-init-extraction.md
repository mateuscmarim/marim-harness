# Harness `__init__` Wiring Extraction Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Extract the collaborator-wiring graph out of the 85-line `Harness.__init__` into a named, unit-testable `build_collaborators()` factory that returns a `Collaborators` container, leaving the constructor to assign its own scalar/model state and unpack the container.

**Architecture:** `Harness.__init__` currently interleaves two concerns: (a) trivial assignment of scalar/model state (`model_label`, `current_model`, `model_id`, wake knobs, one-shot notes) and (b) the collaborator wiring graph with real ordering constraints and the `deps`↔`services` cycle (`Agent`, `McpManager`, `LspManager`, `SessionController`, `CheckpointManager`, `TurnHooks`, `SubagentRunner`, then `build_services`). This plan moves (b) into a module-level `build_collaborators(model, provider, deps, instructions, cfg, *, get_model) -> Collaborators`. The constructor sets its state first, defines the live `get_model` closure (so a runtime `/model` switch is still tracked), then calls the factory and unpacks the result onto `self`. Exact construction order is preserved.

**Tech Stack:** Python 3, dataclasses, pydantic-ai, pytest (`pytest-anyio`), `uv`.

## Global Constraints

- Run tests with `uv run pytest`.
- **No behavioral change.** Construction order and every collaborator's constructor arguments stay byte-for-byte identical — this is a pure extraction.
- The `SubagentRunner`'s `get_model=lambda: self.current_model` must keep reading the **live** `self.current_model`, because `Harness.set_model` reassigns it at runtime. The factory therefore receives `get_model` as a parameter (the closure is defined in `__init__` over `self`), never rebuilt inside the factory.
- The factory must call `build_services(deps, ...)` itself — the `deps.services` late binding is part of the wiring graph being extracted.
- `Harness` keeps these instance attributes exactly as before (other methods read them): `self.agent`, `self.provider`, `self.mcp`, `self.lsp`, `self.session`, `self.checkpoints`, `self.hooks`, `self.subagents`, `self.deps`, `self.model_label`, `self.current_model`, `self.model_source`, `self.model_id`, `self.autonomous_wake`, `self.wake_depth_cap`, `self._pending_error_note`, `self._pending_hook_context`.
- `provider` is an input Harness keeps (`self.provider = provider`); the factory uses it to register the agent but does NOT return it in the container.
- The full suite (currently 1229 passed, 1 skipped) must stay green; the existing `test_agent*.py` suites exercise the real construction path through the new factory.

---

### Task 1: Extract `build_collaborators()` and a `Collaborators` container

**Files:**
- Modify: `src/marim_harness/agent.py` — add `Collaborators` dataclass + `build_collaborators()` above `class Harness` (near the existing `build_services` at lines 246-263); rewrite `Harness.__init__` body (lines 270-351).
- Test: `tests/test_agent.py` — add a unit test for `build_collaborators()`.

**Interfaces:**
- Consumes: `HarnessConfig` (agent.py:206), `build_services` (agent.py:246), and the existing imports already used by the constructor — `Agent`, `Deps`, `McpManager`, `LspManager`, `SessionController`, `CheckpointManager`, `GitSnapshotter`, `TurnHooks`, `SubagentRunner`, `register_instructions`, `_DEFAULT_MODEL_SETTINGS`, `DeferredToolRequests`.
- Produces:
  - `Collaborators` — a frozen dataclass in `marim_harness.agent` with fields `agent: Agent`, `mcp: McpManager`, `lsp: Optional[LspManager]`, `session: SessionController`, `checkpoints: CheckpointManager`, `hooks: TurnHooks`, `subagents: SubagentRunner`.
  - `build_collaborators(model, provider: ToolProvider, deps: Deps, instructions: str, cfg: HarnessConfig, *, get_model: Callable[[], Any]) -> Collaborators` — builds the full graph in the current order, registers `provider` on the agent, calls `build_services(deps, ...)`, and returns the populated container.

- [ ] **Step 1: Write the failing test**

Add to `tests/test_agent.py` (it already constructs `Deps`; reuse the existing imports and add `build_collaborators`, `Collaborators`, `HarnessConfig` to the `from marim_harness.agent import ...` line — check the file's existing import style and match it):

```python
def test_build_collaborators_wires_full_graph(tmp_path):
    from marim_harness.agent import build_collaborators, Collaborators, HarnessConfig
    from marim_harness.deps import Deps
    from marim_harness.tools.provider import ToolProvider

    deps = Deps(workspace_root=tmp_path)
    provider = ToolProvider()
    model = "test-model"

    collab = build_collaborators(
        model, provider, deps, "instructions", HarnessConfig(lsp_enabled=True),
        get_model=lambda: model,
    )

    # Container is fully populated.
    assert isinstance(collab, Collaborators)
    assert collab.agent is not None
    assert collab.mcp is not None
    assert collab.lsp is not None              # lsp_enabled=True
    assert collab.session is not None
    assert collab.checkpoints is not None
    assert collab.hooks is not None
    assert collab.subagents is not None
    # The deps<->services late binding ran as part of wiring.
    assert deps.services.lsp is collab.lsp
    assert deps.services.turn_hooks is collab.hooks
    assert deps.services.run_subagent == collab.subagents.run
    assert deps.services.run_background_agent == collab.subagents.run_background


def test_build_collaborators_respects_lsp_disabled(tmp_path):
    from marim_harness.agent import build_collaborators, HarnessConfig
    from marim_harness.deps import Deps
    from marim_harness.tools.provider import ToolProvider

    deps = Deps(workspace_root=tmp_path)
    collab = build_collaborators(
        "m", ToolProvider(), deps, "i", HarnessConfig(lsp_enabled=False),
        get_model=lambda: "m",
    )
    assert collab.lsp is None
    assert deps.services.lsp is None
```

Note on `==` vs `is`: `collab.subagents.run` is a bound method, recreated on each attribute access, so `is` would spuriously fail; `==` compares `__func__`+`__self__` and correctly verifies the wiring (same pattern already used in `test_build_services_populates_and_assigns`).

- [ ] **Step 2: Run the test to verify it fails**

Run: `uv run pytest tests/test_agent.py::test_build_collaborators_wires_full_graph tests/test_agent.py::test_build_collaborators_respects_lsp_disabled -v`
Expected: FAIL — `ImportError: cannot import name 'build_collaborators' from 'marim_harness.agent'`.

- [ ] **Step 3: Add `Collaborators` and `build_collaborators()`**

In `src/marim_harness/agent.py`, immediately after the existing `build_services` function (after line 263) and before `class Harness`, add:

```python
@dataclass(frozen=True)
class Collaborators:
    """The wired collaborator graph for one Harness. Built by
    ``build_collaborators`` so the construction order and the deps<->services
    cycle live in one named, testable place rather than inline in
    ``Harness.__init__``."""

    agent: Agent
    mcp: McpManager
    lsp: Optional[LspManager]
    session: SessionController
    checkpoints: CheckpointManager
    hooks: TurnHooks
    subagents: SubagentRunner


def build_collaborators(
    model,
    provider: ToolProvider,
    deps: Deps,
    instructions: str,
    cfg: HarnessConfig,
    *,
    get_model: Callable[[], Any],
) -> Collaborators:
    """Build and wire the full collaborator graph for a Harness, in dependency
    order, and install the deps<->services binding via ``build_services``.

    ``get_model`` is supplied by the caller (closing over the live
    ``Harness.current_model``) so a runtime ``/model`` switch is tracked
    without rewiring the sub-agent runner.
    """
    agent = Agent(
        model,
        deps_type=Deps,
        instructions=instructions,
        output_type=[str, DeferredToolRequests],
        # One extra retry past pydantic-ai's default of 1: weaker models
        # often need a second attempt to correct a malformed tool argument
        # before the turn fails with UnexpectedModelBehavior.
        retries=2,
        model_settings=_DEFAULT_MODEL_SETTINGS,
    )
    provider.register(agent)
    mcp = McpManager(cfg.mcp_servers or [], set(cfg.mcp_disabled or []))
    register_instructions(agent, mcp, cfg.proactive_memory)
    # Session-scoped LSP server pool, reachable by the navigation/diagnostics
    # tools through deps. Subagents share this deps object, so they get LSP too.
    lsp = LspManager(deps.workspace_root) if cfg.lsp_enabled else None
    session = SessionController(
        cfg.store, cfg.manager, deps,
        cfg.max_context_tokens, cfg.keep_last_messages,
        cfg.summarizer, cfg.titler,
    )
    # Per-session checkpoints. Wire the real GitSnapshotter so rewind
    # restores working-tree files end-to-end.
    checkpoints = CheckpointManager(session, GitSnapshotter(deps.workspace_root))
    hooks = TurnHooks(deps, session)
    # The spawn_agent tool reaches the runner through Deps, the same way
    # other tools reach shared state. The runner reads the current model via
    # the closure, so a runtime /model switch is tracked without rewiring.
    subagents = SubagentRunner(
        provider, mcp, deps, hooks, session,
        get_model=get_model,
        model_settings=_DEFAULT_MODEL_SETTINGS,
        request_limit=cfg.subagent_request_limit,
        build_model=(
            # Bind the narrowed (non-None) source as a default so the
            # deferred closure keeps it typed; ``cfg.model_source`` alone
            # wouldn't narrow inside a lambda called later.
            (lambda mid, _src=cfg.model_source: _src.build(mid))
            if cfg.model_source is not None else None
        ),
    )
    # One cohesive late binding for the collaborator cycle: TurnHooks and the
    # sub-agent runners hold this deps object, and tools reach them back
    # through ctx.deps.services.
    build_services(deps, lsp=lsp, turn_hooks=hooks, subagents=subagents)
    return Collaborators(
        agent=agent, mcp=mcp, lsp=lsp, session=session,
        checkpoints=checkpoints, hooks=hooks, subagents=subagents,
    )
```

Then ensure `Callable` and `Any` are imported at the top of `agent.py` (check the existing `from typing import ...` line; add whichever is missing).

- [ ] **Step 4: Rewrite `Harness.__init__` to use the factory**

Replace the entire body of `Harness.__init__` (currently lines 279-351, from `cfg = config or HarnessConfig(**kwargs)` through the `build_services(...)` call) with:

```python
        cfg = config or HarnessConfig(**kwargs)
        self.deps = deps
        self.provider = provider
        self.model_label = cfg.model_label
        # The model object used for each turn (swappable at runtime), the source
        # that builds new ones, and the id of the active model.
        self.current_model = model
        self.model_source = cfg.model_source
        self.model_id = cfg.model_id
        # Surfaced for the TUI wake scheduler (interactive only).
        self.autonomous_wake = cfg.autonomous_wake
        self.wake_depth_cap = cfg.wake_depth_cap
        # A one-shot note about the last actionable failure, prepended to the
        # next turn's prompt so the model knows it didn't complete (see
        # _actionable_error_note). None when there's nothing to surface.
        self._pending_error_note: Optional[str] = None
        # One-shot context returned by a SessionStart hook, prepended to the next
        # turn's prompt and consumed there (mirrors _pending_error_note).
        self._pending_hook_context: Optional[str] = None
        # Build the collaborator graph in one named, testable place. get_model
        # closes over self so a runtime /model switch (set_model) is tracked.
        collab = build_collaborators(
            model, provider, deps, instructions, cfg,
            get_model=lambda: self.current_model,
        )
        self.agent = collab.agent
        self.mcp = collab.mcp
        self.lsp = collab.lsp
        self.session = collab.session
        self.checkpoints = collab.checkpoints
        self.hooks = collab.hooks
        self.subagents = collab.subagents
```

Leave the `__init__` signature and docstring (lines 270-278) unchanged.

- [ ] **Step 5: Run the new unit tests to verify they pass**

Run: `uv run pytest tests/test_agent.py::test_build_collaborators_wires_full_graph tests/test_agent.py::test_build_collaborators_respects_lsp_disabled -v`
Expected: PASS.

- [ ] **Step 6: Run the full suite to confirm no behavioral change**

Run: `uv run pytest --no-header -q -o addopts=""`
Expected: PASS — `1231 passed, 1 skipped` (1229 prior + 2 new). Any failure in `test_agent*.py`, `test_session.py`, `test_subagent*.py`, or `test_agent_mcp.py` points at a construction-order or argument drift between the old inline body and the factory — diff the factory against the original lines 280-351 and fix the mismatch.

- [ ] **Step 7: Commit**

```bash
git add src/marim_harness/agent.py tests/test_agent.py
git commit -m "refactor(agent): extract Harness.__init__ wiring into build_collaborators factory"
```

---

## Self-Review

**Spec coverage:**
- "Extract wiring out of `Harness.__init__` into a named factory returning a container" → Task 1 adds `Collaborators` + `build_collaborators`, and the constructor unpacks it. ✔
- "No behavioral change / preserve construction order" → Step 3 reproduces the exact order (agent → register → mcp → instructions → lsp → session → checkpoints → hooks → subagents → build_services); Step 6 runs the full suite as the backstop. ✔
- "Live `get_model` for runtime `/model` switch" → factory takes `get_model` as a parameter; `__init__` passes `lambda: self.current_model`; Global Constraints call this out. ✔
- "Factory owns the `build_services` binding" → called inside `build_collaborators` (Step 3), asserted by the unit test. ✔

**Placeholder scan:** No "TBD"/"handle edge cases"/"similar to" — every step shows the actual code; the constructor rewrite is reproduced in full, not described.

**Type consistency:** `Collaborators` field names (`agent`, `mcp`, `lsp`, `session`, `checkpoints`, `hooks`, `subagents`) are referenced identically in the dataclass, the `return Collaborators(...)` call, the `__init__` unpacking, and the unit test. `build_collaborators`'s signature in the Interfaces block, Step 3, and the test call all match (`model, provider, deps, instructions, cfg, *, get_model`). `build_services` is called with the same keyword args (`lsp`, `turn_hooks`, `subagents`) it already defines.
