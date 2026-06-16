from typing import Literal, Optional, Protocol

from pydantic_ai import Agent, RunContext

from ..deps import Deps
from ..memory import global_scope, project_scope, read_memory, save_memory
from ..skills import find_skill, read_bundled_file, read_skill_body
from ..tasks import Task, summarize
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
            scope: Literal["project", "global"] = "project",
            type: str = "project",
        ) -> str:
            """Save a durable fact to persistent memory so it survives across
            turns and sessions. Make `description` self-contained: it's the only
            line shown in the always-loaded index, so put the actual fact in it
            ("User's name is Mateus Coutinho Marim"), not a label ("the user's
            name"). `body` is the full detail. Use `scope="global"` for facts
            about the user that hold in every workspace, `scope="project"`
            (default) for facts about this codebase. `type` is one of user,
            feedback, project, reference. Before saving, check the memory index
            and reuse the same title to update an existing entry rather than
            adding a duplicate. No approval is needed — this only writes inside
            marim's own memory directory."""
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
        def recall(
            ctx: RunContext[Deps], name: str,
            scope: Literal["project", "global"] = "project",
        ) -> str:
            """Read the full body of a saved memory by `name` (its title or slug,
            as shown in the memory index). `scope` is "project" (default) or
            "global". When an index hook looks relevant to the task but lacks the
            detail you need, recall it before answering. Memory files are not
            reachable through read_file — always use this."""
            sc = (
                global_scope()
                if scope == "global"
                else project_scope(ctx.deps.workspace_root)
            )
            return read_memory(sc, name)

        @agent.tool
        def activate_skill(ctx: RunContext[Deps], name: str) -> str:
            """Load a skill's full instructions by `name`, as listed in the
            skills index. Returns the SKILL.md body plus the skill's absolute
            directory, so you can read its bundled files with read_skill_file and
            run any scripts with bash using that absolute path. Activate a skill
            when the task matches its one-line description, then follow what it
            says."""
            skill = find_skill(ctx.deps.workspace_root, name)
            if skill is None:
                return f"No skill named {name!r}. See the skills index."
            return f"Skill directory: {skill.root}\n\n{read_skill_body(skill)}"

        @agent.tool
        def read_skill_file(ctx: RunContext[Deps], name: str, path: str) -> str:
            """Read a file bundled inside a skill (e.g. `references/REFERENCE.md`
            or `scripts/run.py`), where `path` is relative to the skill's
            directory. Use after activate_skill when its instructions point you at
            a bundled file. Works for skills in any scope, including global ones
            outside the workspace that read_file can't reach."""
            skill = find_skill(ctx.deps.workspace_root, name)
            if skill is None:
                return f"No skill named {name!r}. See the skills index."
            return read_bundled_file(skill, path)

        @agent.tool
        def update_tasks(ctx: RunContext[Deps], tasks: list[Task]) -> str:
            """Maintain your checklist for the current multi-step task. Pass the
            FULL list every time — it replaces the previous one. Each item is
            {text, status} where status is pending, in_progress, or done. Keep
            exactly one item in_progress, and mark items done as you finish them.
            Use this for non-trivial work spanning several steps so progress is
            visible; skip it for single-step requests. No approval is needed."""
            ctx.deps.tasks.replace(tasks)
            return summarize(ctx.deps.tasks.items)

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
