from typing import Literal

from pydantic_ai import RunContext

from ..runtime.deps import Deps
from ..workspace.memory import global_scope, project_scope, read_memory, save_memory


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
        else project_scope(ctx.deps.workspace.root)
    )
    path = save_memory(
        sc, name=title, description=description,
        mem_type=type, body=body, title=title,
    )
    return f"Saved {sc.name} memory to {path.name}"


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
        else project_scope(ctx.deps.workspace.root)
    )
    return read_memory(sc, name)
