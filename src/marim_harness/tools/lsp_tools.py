from pydantic_ai import RunContext
from pydantic_ai.toolsets import FunctionToolset

from ..runtime.deps import Deps

_LSP_UNAVAILABLE = "LSP is not available in this session."


async def goto_definition(ctx: RunContext[Deps], path: str, line: int, col: int) -> str:
    """Jump to where the symbol at `path:line:col` is defined, returning the
    target location(s) as `path:line:col`. Coordinates are 1-based — read them
    off `read_file`/`grep` output. Prefer this over grepping for a definition."""
    if ctx.deps.services.lsp is None:
        return _LSP_UNAVAILABLE
    return await ctx.deps.services.lsp.goto_definition(path, line, col)


async def find_references(ctx: RunContext[Deps], path: str, line: int, col: int) -> str:
    """List every use of the symbol at `path:line:col` across the project, as
    `path:line:col` lines. Coordinates are 1-based. Use before renaming or
    removing a symbol to see its blast radius."""
    if ctx.deps.services.lsp is None:
        return _LSP_UNAVAILABLE
    return await ctx.deps.services.lsp.find_references(path, line, col)


async def hover(ctx: RunContext[Deps], path: str, line: int, col: int) -> str:
    """Show the type/signature and docs for the symbol at `path:line:col`
    (1-based), as the language server's hover text. Use to learn a value's type
    without opening its definition."""
    if ctx.deps.services.lsp is None:
        return _LSP_UNAVAILABLE
    return await ctx.deps.services.lsp.hover(path, line, col)


async def document_symbols(ctx: RunContext[Deps], path: str) -> str:
    """Outline one file: its classes, functions, and methods with line numbers.
    A fast way to understand a file's shape before reading it in full."""
    if ctx.deps.services.lsp is None:
        return _LSP_UNAVAILABLE
    return await ctx.deps.services.lsp.document_symbols(path)


async def workspace_symbols(ctx: RunContext[Deps], query: str) -> str:
    """Find a symbol by name across the whole project, returning matches as
    `name  path:line`. Use to locate a class/function when you know its name but
    not its file."""
    if ctx.deps.services.lsp is None:
        return _LSP_UNAVAILABLE
    return await ctx.deps.services.lsp.workspace_symbols(query)


async def diagnostics(ctx: RunContext[Deps], path: str) -> str:
    """Report errors and warnings for `path`, as `path:line:col: severity: message`.
    Edits already append fresh diagnostics automatically; call this to re-check a
    file on demand. For Python this runs a full check (ruff plus, when available,
    pyright type-checking) — deeper than the fast lint that rides on each edit."""
    if ctx.deps.services.lsp is None:
        return _LSP_UNAVAILABLE
    return await ctx.deps.services.lsp.diagnostics(path, deep=True)


def build_lsp_toolset() -> FunctionToolset[Deps]:
    """The six LSP navigation tools as a single deferrable toolset for the main
    agent. Ungated (they are read-only). Registered here rather than via
    ``agent.tool`` so they can ride the per-turn tool-search deferral path
    (see ``runtime.toolsets.compose_turn_toolsets``). Each tool still guards
    ``ctx.deps.services.lsp is None``, so a toolset built while the manager is
    unavailable degrades gracefully. Sub-agents do NOT use this — they keep
    name-based registration (``provider.register_subagent``)."""
    ts: FunctionToolset[Deps] = FunctionToolset()
    ts.add_function(goto_definition)
    ts.add_function(find_references)
    ts.add_function(hover)
    ts.add_function(document_symbols)
    ts.add_function(workspace_symbols)
    ts.add_function(diagnostics)
    return ts
