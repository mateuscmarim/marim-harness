"""marim-harness: a terminal coding agent, embeddable as an agent SDK.

Public SDK surface (lazy — importing marim_harness stays cheap; pydantic_ai
loads only when a symbol is first touched):

    from marim_harness import HarnessBuilder, BuilderError, Mode, Deps
"""

from typing import Any

_LAZY = {
    "HarnessBuilder": ("marim_harness.runtime.builder", "HarnessBuilder"),
    "BuilderError": ("marim_harness.runtime.builder", "BuilderError"),
    # Deps is part of the embedding surface: every custom tool's first
    # parameter is RunContext[Deps], so embedders need it importable without
    # knowing the runtime package layout.
    "Deps": ("marim_harness.runtime.deps", "Deps"),
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
