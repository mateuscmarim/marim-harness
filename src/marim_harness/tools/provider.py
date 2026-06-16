from typing import Optional, Protocol

from pydantic_ai import Agent, RunContext

from ..deps import Deps
from ..memory import global_scope, project_scope, read_memory, save_memory
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
        def tree(ctx: RunContext[Deps], path: str = ".", depth: int = 2) -> str:
            """Show a directory tree. `depth=1` lists one level (like ls); higher
            descends further. Noise dirs (.git, node_modules, …) aren't expanded."""
            return fs.tree(ctx.deps.workspace_root, path, depth)

        @agent.tool
        def grep(ctx: RunContext[Deps], pattern: str, path: Optional[str] = None) -> str:
            """Search file contents for a regex. Optionally scope to `path`."""
            return fs.grep(ctx.deps.workspace_root, pattern, path)

        @agent.tool
        def remember(
            ctx: RunContext[Deps],
            title: str,
            description: str,
            body: str,
            scope: str = "project",
            type: str = "project",
        ) -> str:
            """Save a durable fact to persistent memory so it survives across
            turns and sessions. `scope` is "project" (this codebase, default) or
            "global" (about the user, every workspace). `type` is one of user,
            feedback, project, reference. `description` is a one-line hook shown
            in the always-loaded index; `body` is the full fact. No approval is
            needed — this only writes inside marim's own memory directory."""
            sc = (
                global_scope()
                if scope == "global"
                else project_scope(ctx.deps.workspace_root)
            )
            path = save_memory(
                sc, name=title, description=description,
                mem_type=type, body=body, title=title,
            )
            return f"Saved {sc.name} memory to {path.name}"

        @agent.tool
        def recall(ctx: RunContext[Deps], name: str, scope: str = "project") -> str:
            """Read the full body of a saved memory by `name` (its title or slug,
            as shown in the memory index). `scope` is "project" (default) or
            "global". Use this to expand an index entry — memory files are not
            reachable through read_file."""
            sc = (
                global_scope()
                if scope == "global"
                else project_scope(ctx.deps.workspace_root)
            )
            return read_memory(sc, name)

        @agent.tool(requires_approval=True)
        def write_file(ctx: RunContext[Deps], path: str, content: str) -> str:
            """Create or overwrite a file. `path` is relative to the workspace root."""
            return fs.write_file(ctx.deps.workspace_root, path, content)

        @agent.tool(requires_approval=True)
        def edit_file(
            ctx: RunContext[Deps], path: str, edits: list[fs.Edit]
        ) -> str:
            """Apply one or more find/replace edits to a file, in order and
            all-or-nothing. Each edit is {old_string, new_string, replace_all?};
            old_string must match exactly once unless replace_all is set."""
            return fs.edit_file(ctx.deps.workspace_root, path, edits)

        @agent.tool(requires_approval=True, timeout=_BASH_TIMEOUT)
        async def bash(ctx: RunContext[Deps], command: str) -> str:
            """Run a shell command in the workspace root."""
            return await shell.run_bash(ctx.deps.workspace_root, command)
