# Embeddable SDK: HarnessBuilder — Phase 1 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** A programmatic `HarnessBuilder` so other Python projects can compose a marim `Harness` explicitly — chosen tool groups, custom tools, programmatic MCP/sub-agents/memory/skills — with `bootstrap.build_harness` refactored to drive it (dogfooding).

**Architecture:** Re-front the existing construction seams. `BuiltinToolProvider` gains a `ToolGroups` toggle set; `HarnessConfig` gains four small seams (`extra_agents`, `forge_backend`, `global_instructions`, plus existing knobs); `WorkspaceConfig` gains `memory_root`/`skill_dirs`; a new `runtime/builder.py` assembles it all and validates at `build()`. No changes to the turn loop, approval rounds, or resumability code.

**Tech Stack:** Python ≥3.10, pydantic-ai, pytest, ruff, pyright. Spec: `docs/superpowers/specs/2026-07-08-embeddable-sdk-builder-design.md`.

## Global Constraints

- `requires-python >= 3.10` — no 3.11+-only syntax.
- Use `uv` for everything: `uv run pytest`, `uv run ruff check src tests`, `uv run pyright`.
- Ruff line length 100; lint set `E,F,I,UP,B,SIM`.
- The SDK never reads `MARIM_*` env vars; string model ids resolve via pydantic-ai's own conventions.
- Bare `.build()`: files_read + files_write only, mode `auto`, in-memory session, nothing written to XDG.
- All-defaults `ToolGroups()` (every field True) must reproduce today's `register()` exactly — CLI back-compat.
- Preserve the long "why" comments around resumability and the deps/services cycle when editing nearby code.
- Phase 2 (`stream_turn`) is OUT of scope.

---

### Task 1: `ToolGroups` + group map + provider gating

**Files:**
- Modify: `src/marim_harness/tools/names.py`
- Modify: `src/marim_harness/tools/provider.py`
- Test: `tests/test_provider.py`

**Interfaces:**
- Produces: `names.TOOL_GROUPS: dict[str, frozenset[str]]`; `provider.ToolGroups` frozen dataclass (9 bool fields, all default True) with method `enabled_tool_names() -> frozenset[str]`; `BuiltinToolProvider(groups: ToolGroups | None = None, *, register_lsp_tools=True, combined_job_tool=False)`.

- [ ] **Step 1: Write the failing tests** (append to `tests/test_provider.py`):

```python
def test_tool_groups_match_dataclass_fields():
    """Every ToolGroups field has a names.TOOL_GROUPS entry and vice versa."""
    import dataclasses
    from marim_harness.tools.names import TOOL_GROUPS
    from marim_harness.tools.provider import ToolGroups

    assert {f.name for f in dataclasses.fields(ToolGroups)} == set(TOOL_GROUPS)


def test_all_groups_on_matches_legacy_registration():
    """ToolGroups() with all defaults registers exactly what the no-arg provider does."""
    from marim_harness.tools.provider import ToolGroups

    legacy = Agent(TestModel(), deps_type=Deps)
    BuiltinToolProvider().register(legacy)
    grouped = Agent(TestModel(), deps_type=Deps)
    BuiltinToolProvider(groups=ToolGroups()).register(grouped)
    assert _tool_names(legacy) == _tool_names(grouped)


def test_bare_groups_register_only_file_tools():
    from marim_harness.tools.provider import ToolGroups

    groups = ToolGroups(bash=False, net=False, memory=False, skills=False,
                        tasks=False, jobs=False, spawn=False)
    agent = Agent(TestModel(), deps_type=Deps)
    BuiltinToolProvider(groups=groups).register(agent)
    assert _tool_names(agent) == {
        "read_file", "glob", "tree", "grep", "write_file", "edit_file",
    }


def test_each_group_toggles_exactly_its_tools():
    """Turning one group off removes exactly that group's tools (jobs uses the
    non-combined variant, so 'job' is excluded from the expectation)."""
    import dataclasses
    from marim_harness.tools.names import TOOL_GROUPS
    from marim_harness.tools.provider import ToolGroups

    baseline_agent = Agent(TestModel(), deps_type=Deps)
    BuiltinToolProvider(groups=ToolGroups()).register(baseline_agent)
    baseline = _tool_names(baseline_agent)
    for field_ in dataclasses.fields(ToolGroups):
        agent = Agent(TestModel(), deps_type=Deps)
        groups = ToolGroups(**{field_.name: False})
        BuiltinToolProvider(groups=groups).register(agent)
        removed = baseline - _tool_names(agent)
        assert removed == (TOOL_GROUPS[field_.name] & baseline), field_.name


def test_enabled_tool_names_unions_active_groups():
    from marim_harness.tools.provider import ToolGroups

    groups = ToolGroups(bash=False, net=False, memory=False, skills=False,
                        tasks=False, jobs=False, spawn=False)
    assert groups.enabled_tool_names() == frozenset(
        {"read_file", "glob", "tree", "grep", "write_file", "edit_file"}
    )
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest --no-cov tests/test_provider.py -v -k "groups or enabled_tool_names"`
Expected: FAIL — `ImportError: cannot import name 'ToolGroups'` / `TOOL_GROUPS`.

- [ ] **Step 3: Add `TOOL_GROUPS` to `names.py`** (append; names.py stays a pure-data leaf):

```python
# Composition groups for the embeddable builder (see runtime/builder.py). Keys
# MUST mirror provider.ToolGroups' field names — test_provider asserts this.
# "jobs" lists both the four split tools and the combined "job" variant; the
# provider registers one shape or the other, but both belong to the group.
TOOL_GROUPS: dict[str, frozenset[str]] = {
    "files_read": frozenset({"read_file", "glob", "tree", "grep"}),
    "files_write": frozenset({"write_file", "edit_file"}),
    "bash": frozenset({"bash"}),
    "net": NET_TOOLS,
    "memory": frozenset({"remember", "recall"}),
    "skills": frozenset({"activate_skill", "read_skill_file"}),
    "tasks": frozenset({"update_tasks", "ask_user", "present_plan"}),
    "jobs": frozenset({"jobs", "job_output", "wait_for_job", "cancel_job", "job"}),
    "spawn": frozenset({"spawn_agent"}),
}
```

- [ ] **Step 4: Add `ToolGroups` and gate `register()` in `provider.py`.**

Add after the imports (import `dataclass` from `dataclasses`, and `TOOL_GROUPS` in the existing `from .names import` block):

```python
@dataclass(frozen=True)
class ToolGroups:
    """Which built-in tool groups a provider registers. All-on defaults keep the
    CLI's historical behavior; the HarnessBuilder passes an explicit selection.
    Field names mirror names.TOOL_GROUPS keys (test-enforced)."""

    files_read: bool = True
    files_write: bool = True
    bash: bool = True
    net: bool = True
    memory: bool = True
    skills: bool = True
    tasks: bool = True
    jobs: bool = True
    spawn: bool = True

    def enabled_tool_names(self) -> frozenset[str]:
        """Union of the tool names in every enabled group (both job-tool shapes
        included — collision checks want the superset)."""
        names: frozenset[str] = frozenset()
        for group, tools in TOOL_GROUPS.items():
            if getattr(self, group):
                names |= tools
        return names
```

Change `BuiltinToolProvider.__init__` to accept `groups`:

```python
    def __init__(self, groups: ToolGroups | None = None, *,
                 register_lsp_tools: bool = True,
                 combined_job_tool: bool = False) -> None:
```

(keep the existing docstring, adding one line: `` `groups` selects which built-in tool groups register() installs; None means all — the CLI's historical behavior.``) and store `self._groups = groups or ToolGroups()`.

Rewrite the body of `register()` keeping every existing registration line and comment, wrapped per group:

```python
    def register(self, agent: HarnessAgent) -> None:
        """Register the enabled main-agent tool groups: read tools, the memory /
        skill / task / spawn tools, and the workspace-mutating tools behind
        approval. Group selection comes from ``ToolGroups`` (all-on by default)."""
        g = self._groups
        # Registered individually rather than via a loop: each tool has a distinct
        # signature, and a loop variable unions them into a type the .tool()
        # overloads can't resolve.
        if g.files_read:
            agent.tool(fs_tools.read_file)
            agent.tool(fs_tools.glob)
            agent.tool(fs_tools.tree)
            agent.tool(fs_tools.grep)
        # Outbound network tools are gated (like write/edit/bash), not ungated
        # like the local reads above: they are an exfiltration boundary (see
        # names.NET_TOOLS). Gating routes them through resolve_approvals, so auto
        # mode still runs them un-prompted (frictionless), ask mode prompts per
        # call, and — the point — plan mode denies them instead of silently
        # allowing an un-approved fetch that could carry a secret off the host.
        if g.net:
            agent.tool(requires_approval=True)(net_tools.web_search)
            agent.tool(requires_approval=True)(net_tools.fetch_url)
        if g.memory:
            agent.tool(memory_tools.remember)
            agent.tool(memory_tools.recall)
        if g.skills:
            agent.tool(skill_tools.activate_skill)
            agent.tool(skill_tools.read_skill_file)
        if g.tasks:
            agent.tool(planning_tools.update_tasks)
            agent.tool(planning_tools.ask_user)
            agent.tool(planning_tools.present_plan)
        # The nesting ceiling isn't bound here: it rides on Deps
        # (subagent_max_depth), where the model can't touch it.
        if g.spawn:
            agent.tool(spawn_tools.spawn_agent)
        if g.jobs:
            if self._combined_job_tool:
                agent.tool(job_tools.job)
            else:
                agent.tool(job_tools.jobs)
                agent.tool(job_tools.job_output)
                agent.tool(job_tools.wait_for_job)
                agent.tool(job_tools.cancel_job)
        if g.files_write:
            agent.tool(requires_approval=True)(edit_tools.write_file)
            agent.tool(requires_approval=True)(edit_tools.edit_file)
        if g.bash:
            agent.tool(requires_approval=True)(edit_tools.bash)
```

- [ ] **Step 5: Run the new tests, then the whole provider file**

Run: `uv run pytest --no-cov tests/test_provider.py -v`
Expected: PASS (all, including pre-existing).

- [ ] **Step 6: Lint, type-check, commit**

```bash
uv run ruff check src tests && uv run pyright
git add src/marim_harness/tools/names.py src/marim_harness/tools/provider.py tests/test_provider.py
git commit -m "feat(tools): ToolGroups composition on BuiltinToolProvider"
```

---

### Task 2: `memory_root` and `skill_dirs` workspace knobs

**Files:**
- Modify: `src/marim_harness/runtime/deps.py` (WorkspaceConfig, ~line 122)
- Modify: `src/marim_harness/tools/memory_tools.py`
- Modify: `src/marim_harness/workspace/skills.py` (`discover_skills`, `find_skill`)
- Modify: `src/marim_harness/tools/skill_tools.py`
- Test: `tests/test_workspace_knobs.py` (new)

**Interfaces:**
- Consumes: `MemoryScope`, `global_scope`, `project_scope` from `workspace/memory.py`.
- Produces: `WorkspaceConfig.memory_root: Path | None = None`, `WorkspaceConfig.skill_dirs: tuple[Path, ...] | None = None`; `discover_skills(workspace_root, *, trust_project=None, dirs=None)`; `find_skill(workspace_root, name, *, dirs=None)`; `memory_tools.resolve_scope(ctx, which)`.

- [ ] **Step 1: Write the failing tests** (`tests/test_workspace_knobs.py`):

```python
from pathlib import Path
from types import SimpleNamespace

from marim_harness.runtime.deps import Deps, WorkspaceConfig
from marim_harness.tools import memory_tools
from marim_harness.workspace.skills import discover_skills, find_skill


def _ctx(workspace: Path, **kw) -> SimpleNamespace:
    return SimpleNamespace(deps=Deps(workspace=WorkspaceConfig(root=workspace, **kw)))


def test_memory_root_overrides_default_scopes(tmp_path: Path):
    store = tmp_path / "memstore"
    ctx = _ctx(tmp_path / "ws", memory_root=store)
    g = memory_tools.resolve_scope(ctx, "global")
    p = memory_tools.resolve_scope(ctx, "project")
    assert g.root == store / "global"
    assert p.root == store / "project"


def test_memory_default_scopes_unchanged(tmp_path: Path):
    ctx = _ctx(tmp_path / "ws")
    p = memory_tools.resolve_scope(ctx, "project")
    assert p.root == tmp_path / "ws" / ".marim" / "memory"


def _write_skill(root: Path, name: str) -> None:
    d = root / name
    d.mkdir(parents=True)
    (d / "SKILL.md").write_text(
        f"---\nname: {name}\ndescription: a test skill\n---\nbody of {name}\n"
    )


def test_explicit_skill_dirs_replace_discovery(tmp_path: Path):
    explicit = tmp_path / "sk"
    _write_skill(explicit, "alpha")
    ws = tmp_path / "ws"
    (ws / ".marim" / "skills").mkdir(parents=True)
    _write_skill(ws / ".marim" / "skills", "hidden")
    found = discover_skills(ws, dirs=(explicit,))
    assert [s.name for s in found] == ["alpha"]
    assert find_skill(ws, "alpha", dirs=(explicit,)) is not None
    assert find_skill(ws, "hidden", dirs=(explicit,)) is None
```

(If the real SKILL.md frontmatter needs different keys, mirror an existing fixture from the skills tests — check `grep -rn "SKILL.md" tests/ | head` and copy that shape.)

- [ ] **Step 2: Run to verify failure**

Run: `uv run pytest --no-cov tests/test_workspace_knobs.py -v`
Expected: FAIL — `WorkspaceConfig` has no `memory_root`; `resolve_scope` undefined; `dirs` unexpected kwarg.

- [ ] **Step 3: Implement.**

`deps.py` — append to `WorkspaceConfig` (after `tool_search_threshold`):

```python
    # Embedder overrides (set by HarnessBuilder; None everywhere in the CLI):
    # an explicit memory store root replacing the XDG-global/.marim-project
    # scopes, and explicit skill directories replacing skill discovery.
    memory_root: Path | None = None
    skill_dirs: "tuple[Path, ...] | None" = None
```

`memory_tools.py` — add (importing `MemoryScope` alongside the existing memory imports):

```python
def resolve_scope(ctx: RunContext[Deps], which: str) -> MemoryScope:
    """Pick the memory scope for ``which`` ("global" | "project"). An explicit
    ``workspace.memory_root`` (embedders, via HarnessBuilder.with_memory) maps
    both scopes under one root; otherwise the CLI defaults apply."""
    root = ctx.deps.workspace.memory_root
    if root is not None:
        return MemoryScope(which, root / which)
    return global_scope() if which == "global" else project_scope(ctx.deps.workspace.root)
```

Then replace the two inline ternaries in `remember` (lines ~29-31) and `recall` (lines ~50-52) with `resolve_scope(ctx, "global" if <existing condition> else "project")` — keep the existing condition expression exactly (it decides global vs project from the tool's `scope` argument).

`workspace/skills.py` — add `dirs` to both entry points:

```python
def discover_skills(workspace_root, *, trust_project: bool | None = None,
                    dirs: "Sequence[Path] | None" = None) -> list[Skill]:
```

At the top of the body: when `dirs is not None`, build `roots = [("explicit", Path(d), None) for d in dirs]` and skip `_all_skill_roots` (plugin/global/project discovery entirely bypassed — that is the point: embedders opt out of scanning). The discovery cache key must include the dirs (the existing `_discovery_signature(roots)` already keys on the roots' contents, so passing the explicit roots through it is sufficient). `find_skill` gains the same `dirs` keyword and passes it through to its discovery call. Import `Sequence` from `collections.abc`.

`skill_tools.py` — thread the knob in both tools:

```python
    skill = find_skill(ctx.deps.workspace.root, name, dirs=ctx.deps.workspace.skill_dirs)
```

Also run `grep -rn "discover_skills(" src/marim_harness/` — any call site with a `RunContext[Deps]`/`Deps` in scope (the skills-index instruction closure in `runtime/instructions.py` in particular) must pass `dirs=ctx.deps.workspace.skill_dirs` too, so the index the model sees matches what activate_skill can load. Call sites without deps access (CLI listings) stay as-is.

- [ ] **Step 4: Run tests**

Run: `uv run pytest --no-cov tests/test_workspace_knobs.py tests/test_provider.py -v` then the skills/memory test files (`uv run pytest --no-cov tests/ -k "skill or memory" -v`).
Expected: PASS.

- [ ] **Step 5: Lint, type-check, commit**

```bash
uv run ruff check src tests && uv run pyright
git add -A src/marim_harness tests/test_workspace_knobs.py
git commit -m "feat(workspace): explicit memory_root and skill_dirs overrides"
```

---

### Task 3: Programmatic sub-agent definitions (`extra_agents`)

**Files:**
- Modify: `src/marim_harness/runtime/harness.py` (HarnessConfig + `build_collaborators`)
- Modify: `src/marim_harness/subagents/runner.py`
- Test: `tests/test_subagent_extra_agents.py` (new)

**Interfaces:**
- Consumes: `AgentDef` from `workspace/agents.py` (frozen dataclass: name, description, prompt, tools, source, plugin=None, backend="native", model=None).
- Produces: `HarnessConfig.extra_agents: tuple = ()`; `SubagentRunner(..., extra_agents=())` and `SubagentRunner._resolve_agent(type_) -> AgentDef | None`.

- [ ] **Step 1: Write the failing test** (`tests/test_subagent_extra_agents.py`). Build a `SubagentRunner` the same way an existing runner unit test does — copy the minimal constructor call from `grep -rn "SubagentRunner(" tests/ | head` and add the new kwarg:

```python
from marim_harness.workspace.agents import AgentDef

REVIEWER = AgentDef(
    name="reviewer", description="reviews diffs", prompt="You review diffs.",
    tools=frozenset({"read_file", "grep"}), source="programmatic",
)


def test_resolve_agent_prefers_extra_defs(subagent_runner_factory):
    runner = subagent_runner_factory(extra_agents=(REVIEWER,))
    assert runner._resolve_agent("reviewer") is REVIEWER
    # Built-ins still resolve through discovery:
    assert runner._resolve_agent("explore") is not None
    assert runner._resolve_agent("no-such-agent") is None
```

If no reusable factory fixture exists, construct the runner inline exactly as the closest existing test does (provider, mcp manager, deps, hooks, session all have cheap test doubles in `tests/conftest.py` / existing runner tests) and pass `extra_agents=(REVIEWER,)`.

- [ ] **Step 2: Run to verify failure**

Run: `uv run pytest --no-cov tests/test_subagent_extra_agents.py -v`
Expected: FAIL — unexpected keyword `extra_agents`.

- [ ] **Step 3: Implement.**

`subagents/runner.py`:
- Add keyword param `extra_agents: "tuple[AgentDef, ...]" = ()` to `SubagentRunner.__init__`; store `self._extra_agents = tuple(extra_agents)`.
- Add the resolver method:

```python
    def _resolve_agent(self, type_: str) -> AgentDef | None:
        """Programmatic defs (HarnessBuilder.with_subagent) take precedence over
        discovered ones, then fall back to workspace/built-in discovery."""
        for d in self._extra_agents:
            if type_ in (d.name, d.qualified_name):
                return d
        return find_agent(self.deps.workspace.root, type_)
```

- Replace all three `find_agent(self.deps.workspace.root, <type>)` call sites (lines ~296, ~699, ~1193) with `self._resolve_agent(<type>)`.
- In the unknown-type error message near line 299, prepend the extras so the model sees them: `[a.qualified_name for a in (*self._extra_agents, *discover_agents(self.deps.workspace.root))]`.

`runtime/harness.py`:
- `HarnessConfig`: add field with comment:

```python
    # Programmatic sub-agent definitions (HarnessBuilder.with_subagent). Resolved
    # ahead of workspace discovery by SubagentRunner._resolve_agent.
    extra_agents: tuple = ()
```

- In `build_collaborators`, pass `extra_agents=cfg.extra_agents` to the `SubagentRunner(...)` call.

- [ ] **Step 4: Run tests**

Run: `uv run pytest --no-cov tests/test_subagent_extra_agents.py tests/ -k "subagent" -v`
Expected: PASS.

- [ ] **Step 5: Lint, type-check, commit**

```bash
uv run ruff check src tests && uv run pyright
git add src/marim_harness/runtime/harness.py src/marim_harness/subagents/runner.py tests/test_subagent_extra_agents.py
git commit -m "feat(subagents): programmatic AgentDefs via HarnessConfig.extra_agents"
```

---

### Task 4: `forge_backend` injection + `global_instructions` gate

**Files:**
- Modify: `src/marim_harness/runtime/harness.py` (HarnessConfig + `build_collaborators`)
- Modify: `src/marim_harness/runtime/instructions.py` (`register_instructions`)
- Test: `tests/test_config_seams.py` (new)

**Interfaces:**
- Consumes: `build_forge_toolset(backend)` from `tools/forge_tools.py`; `ForgeBackend` protocol from `forge/backend.py`.
- Produces: `HarnessConfig.forge_backend: object | None = None`; `HarnessConfig.global_instructions: bool = True`; `register_instructions(agent, mcp_manager, proactive_memory, *, global_instructions=True)`.

- [ ] **Step 1: Write the failing tests** (`tests/test_config_seams.py`):

```python
from pathlib import Path

from pydantic_ai import Agent
from pydantic_ai.models.test import TestModel

from marim_harness.runtime.deps import Deps
from marim_harness.runtime.harness import Harness, HarnessConfig
from marim_harness.tools.provider import BuiltinToolProvider
from tests.conftest import _make_deps  # same helper test_provider uses


def _harness(tmp_path: Path, **cfg_kwargs) -> Harness:
    return Harness(
        TestModel(), BuiltinToolProvider(), _make_deps(tmp_path), "instructions",
        config=HarnessConfig(lsp_enabled=False, **cfg_kwargs),
    )


def test_explicit_forge_backend_attaches_toolset(tmp_path, fake_forge_backend):
    """An explicit backend attaches forge tools even with no tea CLI configured."""
    h = _harness(tmp_path, forge_enabled=True, forge_backend=fake_forge_backend)
    toolset_tools = {n for ts in h.agent.toolsets for n in getattr(ts, "tools", {})}
    assert "list_prs" in toolset_tools  # any forge tool name proves attachment


def test_global_instructions_gate(tmp_path, monkeypatch):
    """global_instructions=False must not read the user-level instructions file."""
    import marim_harness.runtime.instructions as instr

    calls = []
    monkeypatch.setattr(instr, "load_global_instructions",
                        lambda: calls.append(1) or "")
    _harness(tmp_path, global_instructions=False)
    # Registration is closure-based; force instruction evaluation is not needed —
    # with the gate off the closure must not even be registered. Assert via the
    # agent's instruction functions count vs a gated-on harness:
    h_on = _harness(tmp_path, global_instructions=True)
    h_off = _harness(tmp_path, global_instructions=False)
    assert len(h_off.agent._instructions_functions) == \
        len(h_on.agent._instructions_functions) - 1
```

(`_instructions_functions` is pydantic-ai's internal list of `@agent.instructions`
closures — verify the attribute name against the installed version with
`uv run python -c "from pydantic_ai import Agent; a=Agent('test'); print([n for n in dir(a) if 'instruction' in n])"`
and adjust the assertion if it differs.)

For `fake_forge_backend`, add a minimal fixture in this file implementing the `ForgeBackend` protocol methods with stubs (copy the protocol's method list from `src/marim_harness/forge/backend.py`; each stub can `raise NotImplementedError` — only attachment is asserted, nothing is called). Verify the real forge tool names with `grep -n "def " src/marim_harness/tools/forge_tools.py` and use one that exists (adjust `"list_prs"` if the actual name differs).

- [ ] **Step 2: Run to verify failure**

Run: `uv run pytest --no-cov tests/test_config_seams.py -v`
Expected: FAIL — unexpected `forge_backend` / `global_instructions` fields.

- [ ] **Step 3: Implement.**

`HarnessConfig` — two new fields with comments:

```python
    # Explicit forge backend (HarnessBuilder.with_forge). When set it bypasses
    # select_backend's tea-on-PATH auto-detection; forge_enabled must still be
    # True for it to attach.
    forge_backend: object | None = None
    # Register the user-level global-instructions closure. The CLI keeps this
    # on; the builder turns it off so an embedded harness never reads the
    # embedding user's marim config dir.
    global_instructions: bool = True
```

`build_collaborators` — replace the `forge_ts = forge_toolsets(...)` line (keep its comment, extend it):

```python
    # Forge (Gitea/GitHub) tools: an explicit backend (embedders) attaches
    # directly; otherwise attach only when enabled AND a backend is available
    # (tea on PATH + a configured login); forge_toolsets returns [] otherwise,
    # making toolsets=[] a no-op on the Agent below.
    if cfg.forge_backend is not None and cfg.forge_enabled:
        from ..tools.forge_tools import build_forge_toolset
        forge_ts = [build_forge_toolset(cfg.forge_backend)]
    else:
        forge_ts = forge_toolsets(cfg.forge_enabled, deps.workspace.root)
```

(Import at top level instead if no cycle results — check with `uv run python -c "import marim_harness.runtime.harness"`.)

Pass the gate through: `register_instructions(agent, mcp, cfg.proactive_memory, global_instructions=cfg.global_instructions)`.

`runtime/instructions.py` — change the signature to

```python
def register_instructions(
    agent: HarnessAgent, mcp_manager: McpManager, proactive_memory: bool,
    *, global_instructions: bool = True,
) -> None:
```

and wrap only the `_global_instructions` closure registration in `if global_instructions:` (the other closures register unconditionally, as today).

- [ ] **Step 4: Run tests**

Run: `uv run pytest --no-cov tests/test_config_seams.py tests/test_forge_wiring.py -v`
Expected: PASS.

- [ ] **Step 5: Lint, type-check, commit**

```bash
uv run ruff check src tests && uv run pyright
git add src/marim_harness/runtime tests/test_config_seams.py
git commit -m "feat(runtime): forge_backend injection and global-instructions gate"
```

---

### Task 5: `HarnessBuilder` core + lazy package exports

**Files:**
- Create: `src/marim_harness/runtime/builder.py`
- Modify: `src/marim_harness/runtime/instructions.py` (add `DEFAULT_INSTRUCTIONS`)
- Modify: `src/marim_harness/runtime/bootstrap.py` (import `DEFAULT_INSTRUCTIONS`, keep `INSTRUCTIONS` alias)
- Modify: `src/marim_harness/__init__.py`
- Test: `tests/test_builder.py` (new)

**Interfaces:**
- Consumes: everything Tasks 1–4 produced.
- Produces: `marim_harness.HarnessBuilder`, `marim_harness.BuilderError` (lazy); builder methods `with_bash(policy=None)`, `with_net()`, `with_memory(dir=None)`, `with_skills(dirs=None)`, `with_tasks()`, `with_jobs(combined=False)`, `with_lsp(tools=True)`, `with_mcp_server(server)`, `with_forge(backend)`, `with_subagent(defn)`, `with_tool(fn, requires_approval=False)`, `with_instructions(extra=None, replace=None)`, `with_sessions(dir=None)`, `with_mode(mode)`, `with_hooks(runner)`, `with_defaults()`, `with_deps(deps)`, `with_config_overrides(**fields)`, `build() -> Harness`.

- [ ] **Step 1: Write the failing tests** (`tests/test_builder.py`):

```python
from pathlib import Path

import pytest
from pydantic_ai import RunContext
from pydantic_ai.models.test import TestModel

from marim_harness import BuilderError, HarnessBuilder
from marim_harness.runtime.deps import Deps
from marim_harness.runtime.permissions import Mode
from marim_harness.workspace.agents import AgentDef


def _tool_names(harness) -> set[str]:
    return set(harness.agent._function_toolset.tools.keys())


def test_bare_build_defaults(tmp_path: Path):
    h = HarnessBuilder(workspace=tmp_path, model=TestModel()).build()
    assert _tool_names(h) == {"read_file", "glob", "tree", "grep",
                              "write_file", "edit_file"}
    assert h.deps.workspace.mode is Mode.auto
    assert h.session.store is None          # in-memory: nothing hits XDG
    assert h.lsp is None
    assert h.provider.lsp_toolset() is None


def test_with_methods_enable_groups(tmp_path: Path):
    h = (HarnessBuilder(workspace=tmp_path, model=TestModel())
         .with_bash().with_net().with_tasks().build())
    names = _tool_names(h)
    assert {"bash", "web_search", "fetch_url", "update_tasks"} <= names
    assert "remember" not in names          # memory still off


def test_custom_tool_registers(tmp_path: Path):
    def deploy(ctx: RunContext[Deps], target: str) -> str:
        """Deploy the app to `target`."""
        return f"deployed {target}"

    h = (HarnessBuilder(workspace=tmp_path, model=TestModel())
         .with_tool(deploy, requires_approval=True).build())
    assert "deploy" in _tool_names(h)


def test_with_subagent_implies_spawn(tmp_path: Path):
    d = AgentDef(name="rev", description="d", prompt="p",
                 tools=frozenset({"read_file"}), source="programmatic")
    h = HarnessBuilder(workspace=tmp_path, model=TestModel()).with_subagent(d).build()
    assert "spawn_agent" in _tool_names(h)
    assert h.subagents._resolve_agent("rev") is d


def test_build_reports_all_problems_at_once(tmp_path: Path):
    def read_file(ctx: RunContext[Deps]) -> str:   # collides with builtin
        """Shadow."""
        return ""

    bad_agent = AgentDef(name="x", description="d", prompt="p",
                         tools=frozenset({"nonexistent_tool"}), source="programmatic")
    with pytest.raises(BuilderError) as exc:
        (HarnessBuilder(workspace=tmp_path, model=TestModel())
         .with_tool(read_file).with_subagent(bad_agent).build())
    msg = str(exc.value)
    assert "read_file" in msg and "nonexistent_tool" in msg   # both reported


def test_build_twice_raises(tmp_path: Path):
    b = HarnessBuilder(workspace=tmp_path, model=TestModel())
    b.build()
    with pytest.raises(RuntimeError):
        b.build()


def test_string_model_unresolvable_is_builder_error(tmp_path: Path):
    with pytest.raises(BuilderError):
        HarnessBuilder(workspace=tmp_path, model="no-such-provider:xyz").build()


def test_sessions_opt_in_writes_under_given_dir(tmp_path: Path):
    sessions = tmp_path / "sess"
    h = (HarnessBuilder(workspace=tmp_path / "ws", model=TestModel())
         .with_sessions(dir=sessions).build())
    assert h.session.store is not None
    assert sessions.exists()


def test_memory_and_skills_knobs_land_on_workspace(tmp_path: Path):
    h = (HarnessBuilder(workspace=tmp_path, model=TestModel())
         .with_memory(dir=tmp_path / "mem")
         .with_skills(dirs=[tmp_path / "sk"]).build())
    assert h.deps.workspace.memory_root == tmp_path / "mem"
    assert h.deps.workspace.skill_dirs == (tmp_path / "sk",)
    assert {"remember", "recall", "activate_skill"} <= _tool_names(h)
```

- [ ] **Step 2: Run to verify failure**

Run: `uv run pytest --no-cov tests/test_builder.py -v`
Expected: FAIL — `ImportError: cannot import name 'HarnessBuilder' from 'marim_harness'`.

- [ ] **Step 3: Move the default instructions.** In `runtime/instructions.py` add:

```python
# The stock system prompt for a built harness. Lives here (not bootstrap) so the
# builder and the CLI share one source; bootstrap re-exports it as INSTRUCTIONS.
DEFAULT_INSTRUCTIONS = (
    "You are a coding agent operating inside a workspace directory. "
    "Use the provided tools to read, search, and edit files and run commands. "
    "Always read a file before editing it. Keep changes minimal and focused."
)
```

In `bootstrap.py` delete the `INSTRUCTIONS = (...)` literal and replace with `from .instructions import DEFAULT_INSTRUCTIONS as INSTRUCTIONS` (keeps any existing importers working).

- [ ] **Step 4: Write `runtime/builder.py`** (complete file):

```python
"""Programmatic Harness construction for embedders.

The builder is the SDK front door: explicit model, explicit composition, no
``MARIM_*`` env reads, nothing written outside the workspace unless opted in.
``bootstrap.build_harness`` (the CLI preset) drives this same builder, so the
two construction paths cannot drift.

Builder methods are dumb chainable setters (no I/O); ``build()`` validates the
whole composition at once and raises ``BuilderError`` listing every problem.
"""

from __future__ import annotations

import dataclasses
from pathlib import Path
from typing import TYPE_CHECKING, Any, Callable

if TYPE_CHECKING:
    from pydantic_ai.models import Model

    from ..hooks import HookRunner
    from ..workspace.agents import AgentDef
    from .harness import Harness

from ..command_policy import CommandPolicy
from .permissions import Mode


class BuilderError(ValueError):
    """Every composition problem ``build()`` found, reported together so the
    embedder fixes one round of errors, not one error per round."""

    def __init__(self, problems: list[str]) -> None:
        self.problems = list(problems)
        super().__init__(
            "invalid harness composition:\n"
            + "\n".join(f"  - {p}" for p in problems)
        )


class HarnessBuilder:
    """Compose a :class:`~marim_harness.runtime.harness.Harness` explicitly.

    Bare ``build()`` gives file read tools plus gated write/edit, mode ``auto``,
    an in-memory session, and nothing else — everything with reach (shell,
    network, LSP, MCP, spawning) is opt-in via ``with_*`` methods.
    """

    def __init__(self, *, workspace: Path, model: "Model | str") -> None:
        self._workspace = Path(workspace)
        self._model: Model | str = model
        self._groups: dict[str, bool] = {"files_read": True, "files_write": True}
        self._command_policy: CommandPolicy | None = None
        self._lsp = False
        self._lsp_tools = False
        self._mcp_servers: list[object] = []
        self._forge_backend: object | None = None
        self._subagents: list[AgentDef] = []
        self._custom_tools: list[tuple[Callable, bool]] = []
        self._instructions_replace: str | None = None
        self._instructions_extra: list[str] = []
        self._sessions_dir: Path | None = None
        self._sessions = False
        self._memory_root: Path | None = None
        self._skill_dirs: tuple[Path, ...] | None = None
        self._mode = Mode.auto
        self._hook_runner: HookRunner | None = None
        self._global_instructions = False
        self._combined_job_tool = False
        self._deps_override = None
        self._config_overrides: dict[str, Any] = {}
        self._built = False

    # -- composition setters (chainable, no I/O) ---------------------------

    def with_bash(self, policy: CommandPolicy | None = None) -> "HarnessBuilder":
        self._groups["bash"] = True
        self._command_policy = policy
        return self

    def with_net(self) -> "HarnessBuilder":
        self._groups["net"] = True
        return self

    def with_memory(self, dir: Path | None = None) -> "HarnessBuilder":
        self._groups["memory"] = True
        self._memory_root = Path(dir) if dir is not None else None
        return self

    def with_skills(self, dirs: "list[Path] | None" = None) -> "HarnessBuilder":
        self._groups["skills"] = True
        self._skill_dirs = tuple(Path(d) for d in dirs) if dirs is not None else None
        return self

    def with_tasks(self) -> "HarnessBuilder":
        self._groups["tasks"] = True
        return self

    def with_jobs(self, *, combined: bool = False) -> "HarnessBuilder":
        self._groups["jobs"] = True
        self._combined_job_tool = combined
        return self

    def with_lsp(self, *, tools: bool = True) -> "HarnessBuilder":
        self._lsp = True
        self._lsp_tools = tools
        return self

    def with_mcp_server(self, server: object) -> "HarnessBuilder":
        """``server`` is a ready pydantic-ai MCP server/toolset object; marim
        JSON specs are a CLI concern (bootstrap converts them before this)."""
        self._mcp_servers.append(server)
        return self

    def with_forge(self, backend: object) -> "HarnessBuilder":
        self._forge_backend = backend
        return self

    def with_subagent(self, defn: "AgentDef") -> "HarnessBuilder":
        self._groups["spawn"] = True  # a spec without spawn_agent is dead weight
        self._subagents.append(defn)
        return self

    def with_tool(self, fn: Callable, *, requires_approval: bool = False) -> "HarnessBuilder":
        self._custom_tools.append((fn, requires_approval))
        return self

    def with_instructions(self, *, extra: str | None = None,
                          replace: str | None = None) -> "HarnessBuilder":
        if replace is not None:
            self._instructions_replace = replace
        if extra is not None:
            self._instructions_extra.append(extra)
        return self

    def with_sessions(self, dir: Path | None = None) -> "HarnessBuilder":
        self._sessions = True
        self._sessions_dir = Path(dir) if dir is not None else None
        return self

    def with_mode(self, mode: Mode) -> "HarnessBuilder":
        self._mode = mode
        return self

    def with_hooks(self, runner: "HookRunner") -> "HarnessBuilder":
        self._hook_runner = runner
        return self

    def with_defaults(self) -> "HarnessBuilder":
        """The full marim toolset: every group, LSP with tools, spawn, jobs,
        and the user-level global instructions. Workspace *scanning* (project
        hooks/MCP/skills discovery) stays with the CLI preset in bootstrap."""
        from ..tools.names import TOOL_GROUPS

        for group in TOOL_GROUPS:
            self._groups[group] = True
        self._lsp = True
        self._lsp_tools = True
        self._global_instructions = True
        return self

    # -- CLI-preset escape hatches (advanced; used by bootstrap) -----------

    def with_deps(self, deps) -> "HarnessBuilder":
        """Replace the builder-constructed Deps wholesale (the CLI preset builds
        its own to wire notifier/tool-search knobs). Overrides with_memory /
        with_skills / with_bash policy placement — the caller owns the object."""
        self._deps_override = deps
        return self

    def with_config_overrides(self, **fields: Any) -> "HarnessBuilder":
        """Set HarnessConfig fields directly (model_source, context_limits,
        store/manager, masking knobs, …). Unstable surface: field names track
        HarnessConfig. Unknown names raise immediately."""
        from .harness import HarnessConfig

        known = {f.name for f in dataclasses.fields(HarnessConfig)}
        unknown = set(fields) - known
        if unknown:
            raise TypeError(f"unknown HarnessConfig fields: {sorted(unknown)}")
        self._config_overrides.update(fields)
        return self

    # -- build --------------------------------------------------------------

    def build(self) -> "Harness":
        # Imports deferred so `import marim_harness` (lazy __getattr__) stays
        # cheap until a builder is actually built.
        from pydantic_ai.models import infer_model

        from ..compaction import make_summarizer, make_titler
        from ..session import SessionManager
        from ..tools.names import LSP_TOOLS, SUBAGENT_TOOLS
        from ..tools.provider import ToolGroups
        from .deps import Deps, WorkspaceConfig
        from .harness import Harness, HarnessConfig

        if self._built:
            raise RuntimeError("this HarnessBuilder already built a Harness; "
                               "create a new builder for a second one")

        problems: list[str] = []

        model = self._model
        if isinstance(model, str):
            try:
                model = infer_model(model)
            except Exception as exc:  # pydantic-ai raises various types here
                problems.append(f"model {self._model!r} is not resolvable: {exc}")

        groups = ToolGroups(**{
            f.name: self._groups.get(f.name, False)
            for f in dataclasses.fields(ToolGroups)
        })
        builtin_names = groups.enabled_tool_names()

        seen_custom: set[str] = set()
        for fn, _gated in self._custom_tools:
            name = fn.__name__
            if name in builtin_names:
                problems.append(f"custom tool {name!r} collides with a built-in tool")
            if name in seen_custom:
                problems.append(f"custom tool {name!r} registered twice")
            seen_custom.add(name)

        grantable = builtin_names | (LSP_TOOLS if self._lsp_tools else frozenset())
        for defn in self._subagents:
            unknown = defn.tools - SUBAGENT_TOOLS
            if unknown:
                problems.append(
                    f"sub-agent {defn.name!r} grants unknown tools: {sorted(unknown)}")
            missing = (defn.tools & SUBAGENT_TOOLS) - grantable - LSP_TOOLS
            if missing:
                problems.append(
                    f"sub-agent {defn.name!r} grants tools from disabled groups: "
                    f"{sorted(missing)}")

        manager = store = None
        if self._sessions:
            try:
                manager = SessionManager(self._workspace, base_dir=self._sessions_dir)
                manager.dir.mkdir(parents=True, exist_ok=True)
            except OSError as exc:
                problems.append(f"sessions dir is not usable: {exc}")
            else:
                store = manager.create()

        if problems:
            raise BuilderError(problems)

        deps = self._deps_override
        if deps is None:
            deps = Deps(workspace=WorkspaceConfig(
                root=self._workspace,
                mode=self._mode,
                command_policy=self._command_policy or CommandPolicy(),
                memory_root=self._memory_root,
                skill_dirs=self._skill_dirs,
            ))
            deps.hooks = self._hook_runner

        from .instructions import DEFAULT_INSTRUCTIONS
        instructions = self._instructions_replace or DEFAULT_INSTRUCTIONS
        if self._instructions_extra:
            instructions = "\n\n".join([instructions, *self._instructions_extra])

        provider = _ComposedProvider(
            groups,
            tuple(self._custom_tools),
            register_lsp_tools=self._lsp and self._lsp_tools,
            combined_job_tool=self._combined_job_tool,
        )

        config_fields: dict[str, Any] = dict(
            lsp_enabled=self._lsp,
            forge_enabled=self._forge_backend is not None,
            forge_backend=self._forge_backend,
            global_instructions=self._global_instructions,
            extra_agents=tuple(self._subagents),
            mcp_servers=list(self._mcp_servers),
            store=store,
            manager=manager,
            summarizer=make_summarizer(model),
            titler=make_titler(model),
        )
        config_fields.update(self._config_overrides)

        self._built = True
        return Harness(model, provider, deps, instructions,
                       config=HarnessConfig(**config_fields))


class _ComposedProvider(BuiltinToolProvider):
    """BuiltinToolProvider plus the embedder's custom tools. Custom gated tools
    ride the exact same requires_approval path as write/edit/bash, so they get
    the full permission model (auto runs, ask prompts, plan denies)."""

    def __init__(self, groups, extra_tools, *, register_lsp_tools, combined_job_tool):
        super().__init__(groups, register_lsp_tools=register_lsp_tools,
                         combined_job_tool=combined_job_tool)
        self._extra_tools = extra_tools

    def register(self, agent) -> None:
        super().register(agent)
        for fn, requires_approval in self._extra_tools:
            if requires_approval:
                agent.tool(requires_approval=True)(fn)
            else:
                agent.tool(fn)
```

Note for the implementer: `_ComposedProvider` needs `from ..tools.provider import BuiltinToolProvider` at module top. `tools.provider` imports `runtime.deps` (not `runtime.builder`), so no cycle is expected — verify with `uv run python -c "import marim_harness.runtime.builder"`. If a cycle does appear, move the class definition inside `build()` instead. Since the top-level import pulls pydantic-ai transitively, keep it under `if TYPE_CHECKING` **only if** the laziness check in Step 6 fails; otherwise plain import is fine (builder.py itself is only imported lazily via `marim_harness.__getattr__`).

- [ ] **Step 5: Lazy exports in `src/marim_harness/__init__.py`** (currently empty):

```python
"""marim-harness: a terminal coding agent, embeddable as an agent SDK.

Public SDK surface (lazy — importing marim_harness stays cheap; pydantic_ai
loads only when a symbol is first touched):

    from marim_harness import HarnessBuilder, BuilderError, Mode
"""

from typing import Any

_LAZY = {
    "HarnessBuilder": ("marim_harness.runtime.builder", "HarnessBuilder"),
    "BuilderError": ("marim_harness.runtime.builder", "BuilderError"),
    "ToolGroups": ("marim_harness.tools.provider", "ToolGroups"),
    "Mode": ("marim_harness.runtime.permissions", "Mode"),
    "CommandPolicy": ("marim_harness.command_policy", "CommandPolicy"),
    "AgentDef": ("marim_harness.workspace.agents", "AgentDef"),
}


def __getattr__(name: str) -> Any:
    try:
        module_name, attr = _LAZY[name]
    except KeyError:
        raise AttributeError(f"module 'marim_harness' has no attribute {name!r}") from None
    import importlib

    return getattr(importlib.import_module(module_name), attr)


def __dir__() -> list[str]:
    return sorted(_LAZY)
```

- [ ] **Step 6: Run the tests**

Run: `uv run pytest --no-cov tests/test_builder.py -v`
Expected: PASS. Also verify laziness: `uv run python -c "import sys, marim_harness; assert 'pydantic_ai' not in sys.modules, 'lazy import broken'"` — expected: no output, exit 0.

- [ ] **Step 7: Lint, type-check, commit**

```bash
uv run ruff check src tests && uv run pyright
git add src/marim_harness/runtime/builder.py src/marim_harness/runtime/instructions.py \
        src/marim_harness/runtime/bootstrap.py src/marim_harness/__init__.py tests/test_builder.py
git commit -m "feat(runtime): HarnessBuilder — programmatic SDK construction"
```

---

### Task 6: End-to-end turn tests for a built harness

**Files:**
- Test: `tests/test_builder_turns.py` (new)

**Interfaces:**
- Consumes: `HarnessBuilder` (Task 5); `Harness.run_turn(prompt) -> str`.

- [ ] **Step 1: Write the tests** (`tests/test_builder_turns.py`):

```python
from pathlib import Path

import pytest
from pydantic_ai import RunContext
from pydantic_ai.messages import ModelResponse, TextPart, ToolCallPart
from pydantic_ai.models.function import AgentInfo, FunctionModel

from marim_harness import HarnessBuilder
from marim_harness.runtime.deps import Deps

pytestmark = pytest.mark.anyio  # match the async marker used in tests/test_agent.py
# (check `grep -rn "anyio\|asyncio" tests/test_agent.py | head` and mirror it)


def _scripted(tool_call_then_text):
    """FunctionModel script: first request calls the tool, second returns text."""
    def call(messages, info: AgentInfo) -> ModelResponse:
        if len(messages) == 1:
            name, args = tool_call_then_text
            return ModelResponse(parts=[ToolCallPart(name, args)])
        return ModelResponse(parts=[TextPart("all done")])
    return FunctionModel(call)


async def test_custom_gated_tool_runs_in_auto_mode(tmp_path: Path):
    calls: list[str] = []

    def deploy(ctx: RunContext[Deps], target: str) -> str:
        """Deploy the app to `target`."""
        calls.append(target)
        return f"deployed {target}"

    harness = (
        HarnessBuilder(workspace=tmp_path,
                       model=_scripted(("deploy", {"target": "prod"})))
        .with_tool(deploy, requires_approval=True)
        .build()
    )
    out = await harness.run_turn("deploy to prod")
    assert calls == ["prod"]          # gated tool executed (auto mode approves)
    assert out == "all done"


async def test_bare_build_reads_files(tmp_path: Path):
    (tmp_path / "hello.txt").write_text("hi")
    harness = HarnessBuilder(
        workspace=tmp_path,
        model=_scripted(("read_file", {"path": "hello.txt"})),
    ).build()
    out = await harness.run_turn("read hello.txt")
    assert out == "all done"


async def test_in_memory_session_round_trips(tmp_path: Path):
    def echo(messages, info: AgentInfo) -> ModelResponse:
        return ModelResponse(parts=[TextPart(f"turn {sum(1 for m in messages)}")])

    harness = HarnessBuilder(workspace=tmp_path, model=FunctionModel(echo)).build()
    first = await harness.run_turn("one")
    second = await harness.run_turn("two")
    assert first != second            # second turn saw a longer history
```

Adjust `read_file`'s argument name to the real tool signature (check `grep -n "def read_file" -A 3 src/marim_harness/tools/fs_tools.py`) and the async marker to whatever `tests/test_agent.py` actually uses before running.

- [ ] **Step 2: Run**

Run: `uv run pytest --no-cov tests/test_builder_turns.py -v`
Expected: PASS. If the gated-tool test hangs or returns without calling `deploy`, the approval resolution isn't treating custom tools like builtins — debug `resolve_approvals` handling in `runtime/controller.py` before touching the test.

- [ ] **Step 3: Commit**

```bash
uv run ruff check tests && git add tests/test_builder_turns.py
git commit -m "test(builder): end-to-end turns through a built harness"
```

---

### Task 7: Dogfood — `build_harness` drives the builder

**Files:**
- Modify: `src/marim_harness/runtime/bootstrap.py`
- Test: existing suites (`tests/test_forge_wiring.py`, `tests/test_lsp_wiring.py`, any test importing `build_harness`) must pass unchanged.

**Interfaces:**
- Consumes: the full builder surface, `with_deps`, `with_config_overrides`.
- Produces: `build_harness` with an identical signature and behavior; it now constructs the Harness via `HarnessBuilder`.

- [ ] **Step 1: Identify the wiring tests that pin current behavior**

Run: `grep -rln "build_harness" tests/` and `uv run pytest --no-cov $(grep -rln "build_harness" tests/) -v` — all green before touching anything. These are the regression net.

- [ ] **Step 2: Refactor `build_harness`.** Keep everything up to and including the MCP/LSP/aux-model sections exactly as-is (config load, provider detection, model build, policy, hooks, notifier, Deps construction, session selection, MCP specs, `register_lsp_tools`, `aux_model`). Then replace the `Harness(...)` construction with the builder:

```python
    from .builder import HarnessBuilder

    builder = (
        HarnessBuilder(workspace=workspace, model=model)
        .with_defaults()                      # full CLI toolset
        .with_deps(deps)                      # CLI-built Deps: notifier, tool-search knobs
        .with_jobs(combined=cfg.job_tool_combined)
        .with_config_overrides(
            # The builder derives forge_enabled from an explicit backend (None
            # here), which would turn CLI forge OFF. Pin the config-driven value
            # so tea auto-detection keeps working — this override must stay.
            forge_enabled=cfg.forge_enabled,
            model_label=model_source.label(model_id),
            store=store,
            manager=manager,
            max_context_tokens=cfg.max_context_tokens,
            context_limits=build_context_limits(
                configs,
                window_override=cfg.context_window,
                budget=cfg.max_context_tokens or None,
                budget_overrides_raw=cfg.context_budgets,
            ),
            mask_observations=cfg.mask_observations,
            mask_keep_recent=cfg.mask_keep_recent,
            mask_min_chars=cfg.mask_min_chars,
            summarizer=make_summarizer(aux_model),
            titler=make_titler(aux_model),
            model_source=model_source,
            model_id=model_id,
            proactive_memory=cfg.proactive_memory,
            autonomous_wake=cfg.subagent.autonomous_wake,
            wake_depth_cap=cfg.subagent.wake_depth_cap,
            subagent_concurrency=cfg.subagent.concurrency,
            subagent_transcript_cap=cfg.subagent.transcript_cap,
            subagent_request_limit=cfg.subagent.request_limit,
            mcp_servers=mcp_servers,
            mcp_disabled=mcp_disabled,
            notifications=cfg.notifications,
        )
    )
    if not register_lsp_tools:
        # with_defaults turned LSP tools on; the CLI's two-switch config may
        # want the manager without the navigation tools (or neither).
        builder._lsp_tools = False
    if not cfg.lsp_enabled:
        builder._lsp = False
    builder._global_instructions = True       # the CLI always reads user config
    harness = builder.build()
```

Preserve the existing comment blocks (window discovery, aux-model isolation) by keeping them attached to the code that moved. If reaching into `builder._lsp*` privates reads badly, add a keyword variant `with_lsp(enabled=True, tools=True)` supporting `enabled=False` instead — implementer's choice; keep whichever is cleaner AND keeps the builder's public API honest (`.with_lsp()` still means "on").

Keep the tail of `build_harness` (resume handling, `_wire_cli_model`) unchanged. Note: the builder's summarizer/titler defaults get overridden by the config overrides here, so the aux-model isolation for claude-cli is preserved.

- [ ] **Step 3: Run the regression net + full suite**

Run: `uv run pytest` (full suite, coverage on).
Expected: PASS, no test edits needed. If a wiring test fails, the builder path diverges from the old construction — fix the builder/bootstrap, never the test.

- [ ] **Step 4: Lint, type-check, commit**

```bash
uv run ruff check src tests && uv run pyright
git add src/marim_harness/runtime/bootstrap.py src/marim_harness/runtime/builder.py
git commit -m "refactor(bootstrap): build_harness drives HarnessBuilder (dogfood)"
```

---

### Task 8: Docs + CI parity

**Files:**
- Create: `docs/embedding.md`
- Modify: `CLAUDE.md` (Architecture section)
- Modify: `README.md` if it has a features list (check first)

- [ ] **Step 1: Write `docs/embedding.md`** — quickstart mirroring the spec's example (bare build, adding groups, custom gated tool, sessions opt-in, `with_defaults`), the ToolGroups table from the spec, the "explicit model / no MARIM_* env / opt-in persistence" rules, and a note that `stream_turn` is planned (link the spec). Keep it under ~120 lines; every code block must be copy-paste runnable against the shipped API.

- [ ] **Step 2: Update `CLAUDE.md`** — in the Architecture section, after the `runtime/` package listing, add `builder.py` to the module list and one short paragraph: `HarnessBuilder` is the embedding front door; `build_harness` is the CLI preset on top of it; new construction wiring goes in the builder, env/discovery reading stays in bootstrap.

- [ ] **Step 3: Full CI parity run**

```bash
uv run ruff check src tests && uv run pyright && uv run pytest
```
Expected: all green — the same order CI runs.

- [ ] **Step 4: Commit**

```bash
git add docs/embedding.md CLAUDE.md README.md
git commit -m "docs: embedding guide for HarnessBuilder"
```

---

## Self-Review Notes

- **Spec coverage:** groups table → Task 1; memory/skills explicit dirs → Task 2; programmatic sub-agents → Task 3; forge backend + no-user-config rule → Task 4; builder API, BuilderError all-at-once, build-twice, lazy exports, custom tools on the approval path, sessions opt-in, instructions extra/replace, with_defaults → Tasks 5–6; dogfooding → Task 7; docs → Task 8. Phase 2 (`stream_turn`) intentionally absent. The spec's "provider-error spill file moves under the session dir" line is deferred: the spill only triggers on provider errors the SDK's in-memory path still writes to `.marim/` — implementer should move `last-provider-error.json` under the session store dir *when a store exists*, else the workspace `.marim/` (one-line change in `runtime/errors.py` or wherever the spill lives; find it with `grep -rn "last-provider-error" src/`). Fold into Task 5 Step 4 if trivial, else raise it at review.
- **Known judgment points for reviewers:** `with_defaults()` does not do workspace scanning (bootstrap keeps that); `with_mcp_server` takes ready server objects only; `builder._lsp*` private pokes in bootstrap may become a `with_lsp(enabled=…)` keyword.
- **Type consistency:** `ToolGroups` field names = `TOOL_GROUPS` keys (test-enforced); `resolve_scope(ctx, which)` used by both memory tools; `_resolve_agent` on SubagentRunner consumed by Task 5's test.
