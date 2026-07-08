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
from collections.abc import Callable
from pathlib import Path
from typing import TYPE_CHECKING, Any, cast

if TYPE_CHECKING:
    from pydantic_ai.models import Model

    from ..hooks import HookRunner
    from ..workspace.agents import AgentDef
    from .harness import Harness

from ..command_policy import CommandPolicy
from ..tools.provider import BuiltinToolProvider
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

    def __init__(self, *, workspace: Path, model: Model | str) -> None:
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

    def with_bash(self, policy: CommandPolicy | None = None) -> HarnessBuilder:
        self._groups["bash"] = True
        self._command_policy = policy
        return self

    def with_net(self) -> HarnessBuilder:
        self._groups["net"] = True
        return self

    def with_memory(self, dir: Path | None = None) -> HarnessBuilder:
        self._groups["memory"] = True
        self._memory_root = Path(dir) if dir is not None else None
        return self

    def with_skills(self, dirs: list[Path] | None = None) -> HarnessBuilder:
        self._groups["skills"] = True
        self._skill_dirs = tuple(Path(d) for d in dirs) if dirs is not None else None
        return self

    def with_tasks(self) -> HarnessBuilder:
        self._groups["tasks"] = True
        return self

    def with_jobs(self, *, combined: bool = False) -> HarnessBuilder:
        self._groups["jobs"] = True
        self._combined_job_tool = combined
        return self

    def with_lsp(self, *, tools: bool = True) -> HarnessBuilder:
        self._lsp = True
        self._lsp_tools = tools
        return self

    def with_mcp_server(self, server: object) -> HarnessBuilder:
        """``server`` is a ready pydantic-ai MCP server/toolset object; marim
        JSON specs are a CLI concern (bootstrap converts them before this)."""
        self._mcp_servers.append(server)
        return self

    def with_forge(self, backend: object) -> HarnessBuilder:
        self._forge_backend = backend
        return self

    def with_subagent(self, defn: AgentDef) -> HarnessBuilder:
        self._groups["spawn"] = True  # a spec without spawn_agent is dead weight
        self._subagents.append(defn)
        return self

    def with_tool(self, fn: Callable, *, requires_approval: bool = False) -> HarnessBuilder:
        self._custom_tools.append((fn, requires_approval))
        return self

    def with_instructions(self, *, extra: str | None = None,
                          replace: str | None = None) -> HarnessBuilder:
        if replace is not None:
            self._instructions_replace = replace
        if extra is not None:
            self._instructions_extra.append(extra)
        return self

    def with_sessions(self, dir: Path | None = None) -> HarnessBuilder:
        self._sessions = True
        self._sessions_dir = Path(dir) if dir is not None else None
        return self

    def with_mode(self, mode: Mode) -> HarnessBuilder:
        self._mode = mode
        return self

    def with_hooks(self, runner: HookRunner) -> HarnessBuilder:
        self._hook_runner = runner
        return self

    def with_defaults(self) -> HarnessBuilder:
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

    def with_deps(self, deps) -> HarnessBuilder:
        """Replace the builder-constructed Deps wholesale (the CLI preset builds
        its own to wire notifier/tool-search knobs). Overrides with_memory /
        with_skills / with_bash policy placement — the caller owns the object."""
        self._deps_override = deps
        return self

    def with_config_overrides(self, **fields: Any) -> HarnessBuilder:
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

    def build(self) -> Harness:
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

        # No problems means the str-model branch above (if taken) resolved
        # cleanly via infer_model — narrow the str | Model union pyright can't
        # follow across the problems-gate for the rest of build().
        model = cast("Model", model)

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
