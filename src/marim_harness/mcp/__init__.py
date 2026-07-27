from typing import TYPE_CHECKING

from .config import (
    build_mcp_servers,
    disabled_server_names,
    global_mcp_config_path,
    load_mcp_config,
    make_approval_hook,
    persist_server_enabled,
    project_mcp_config_path,
)

if TYPE_CHECKING:
    # Real import only for type checkers (see __getattr__ below for the actual
    # lazy runtime binding) — this also satisfies pyright's `__all__` check
    # without paying pydantic_ai's import cost at runtime.
    from .manager import McpManager

__all__ = [
    "McpManager",
    "build_mcp_servers",
    "disabled_server_names",
    "global_mcp_config_path",
    "load_mcp_config",
    "make_approval_hook",
    "persist_server_enabled",
    "project_mcp_config_path",
]


def __getattr__(name: str):
    # PEP 562 lazy attribute: `.manager` imports `pydantic_ai` at module level
    # (CombinedToolset/DeferredLoadingToolset), and importing *any* submodule of
    # this package (e.g. `mcp.config`) runs this `__init__` first — so an eager
    # `from .manager import McpManager` here would drag pydantic_ai onto every
    # consumer of the package, including cheap CLI paths (`marim trust` ->
    # trust_surface -> mcp.config) that only need the config helpers above and
    # never touch McpManager. Deferring the import to first attribute access
    # keeps `from ..mcp import McpManager` working unchanged for the real
    # (pydantic_ai-using) callers.
    if name == "McpManager":
        from .manager import McpManager

        return McpManager
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
