from typing import Literal

from pydantic_ai import RunContext

from ..runtime.deps import Deps
from ..workspace.memory import (
    MemoryScope,
    delete_memory,
    global_scope,
    project_scope,
    read_memory,
    save_memory,
)


def resolve_scope(ctx: RunContext[Deps], which: str) -> MemoryScope:
    """Pick the memory scope for ``which`` ("global" | "project"). An explicit
    ``workspace.memory_root`` (embedders, via HarnessBuilder.with_memory) maps
    both scopes under one root; otherwise the CLI defaults apply."""
    root = ctx.deps.workspace.memory_root
    if root is not None:
        return MemoryScope(which, root / which)
    return global_scope() if which == "global" else project_scope(ctx.deps.workspace.root)


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
    sc = resolve_scope(ctx, "global" if scope == "global" else "project")
    path = save_memory(
        sc, name=title, description=description,
        mem_type=type, body=body, title=title,
    )
    # save_memory fails soft (returns None) rather than raising — an unhandled
    # exception here would abort the whole pydantic-ai run, so a read-only
    # workspace/.marim would otherwise make `remember` turn-killing. Report the
    # failure back to the model as an ordinary tool result instead.
    if path is None:
        return f"Could not save {sc.name} memory — its directory ({sc.root}) is not writable."
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
    sc = resolve_scope(ctx, "global" if scope == "global" else "project")
    return read_memory(sc, name)


def forget(
    ctx: RunContext[Deps], name: str,
    scope: Literal["project", "global"] = "project",
) -> str:
    """Permanently delete a saved memory by `name` (its title or slug, as
    shown in the memory index). Use only when a memory is wrong or
    obsolete; to correct or refresh a fact, prefer remember with the
    same title, which updates the entry in place. `scope` is "project"
    (default) or "global". Check the memory index first so you delete
    the entry you mean — deletion cannot be undone."""
    sc = resolve_scope(ctx, "global" if scope == "global" else "project")
    if delete_memory(sc, name):
        return f"Deleted {sc.name} memory {name!r}."
    return (
        f"No {sc.name} memory named {name!r} to delete "
        "(check the memory index; or its directory is not writable)."
    )
