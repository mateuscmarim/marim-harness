from typing import Optional, Protocol

from pydantic_ai import Agent, RunContext

from ..deps import Deps
from . import fs, shell

_BASH_TIMEOUT = 60


class ToolProvider(Protocol):
    """Registers a set of tools onto an Agent. The swap point for future
    pydantic-ai-harness FileSystem/Shell capabilities."""

    def register(self, agent: Agent) -> None:
        ...


class BuiltinToolProvider:
    """Hand-written fs + shell tools backed by the pure functions in this package."""

    def register(self, agent: Agent) -> None:
        @agent.tool
        def read_file(ctx: RunContext[Deps], path: str) -> str:
            """Read a text file. `path` is relative to the workspace root."""
            return fs.read_file(ctx.deps.workspace_root, path)

        @agent.tool
        def glob(ctx: RunContext[Deps], pattern: str) -> str:
            """List files matching a glob pattern (e.g. `**/*.py`)."""
            return fs.glob_files(ctx.deps.workspace_root, pattern)

        @agent.tool
        def grep(ctx: RunContext[Deps], pattern: str, path: Optional[str] = None) -> str:
            """Search file contents for a regex. Optionally scope to `path`."""
            return fs.grep(ctx.deps.workspace_root, pattern, path)

        @agent.tool(requires_approval=True)
        def write_file(ctx: RunContext[Deps], path: str, content: str) -> str:
            """Create or overwrite a file. `path` is relative to the workspace root."""
            return fs.write_file(ctx.deps.workspace_root, path, content)

        @agent.tool(requires_approval=True)
        def edit_file(
            ctx: RunContext[Deps], path: str, old_string: str, new_string: str
        ) -> str:
            """Replace the unique occurrence of `old_string` with `new_string`."""
            return fs.edit_file(ctx.deps.workspace_root, path, old_string, new_string)

        @agent.tool(requires_approval=True, timeout=_BASH_TIMEOUT)
        async def bash(ctx: RunContext[Deps], command: str) -> str:
            """Run a shell command in the workspace root."""
            return await shell.run_bash(ctx.deps.workspace_root, command)
