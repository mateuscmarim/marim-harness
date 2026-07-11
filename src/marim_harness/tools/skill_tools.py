from pydantic_ai import RunContext

from ..runtime.deps import Deps
from ..workspace.skills import find_skill, read_bundled_file, read_skill_body
from .impl.offload import offload_if_large


def activate_skill(ctx: RunContext[Deps], name: str) -> str:
    """Load a skill's full instructions by `name`, as listed in the
    skills index. Returns the SKILL.md body plus the skill's absolute
    directory, so you can read its bundled files with read_skill_file and
    run any scripts with bash using that absolute path. Activate a skill
    when the task matches its one-line description, then follow what it
    says."""
    skill = find_skill(ctx.deps.workspace.root, name, dirs=ctx.deps.workspace.skill_dirs)
    if skill is None:
        return f"No skill named {name!r}. See the skills index."
    # Offload an oversized SKILL.md through the same guard read_file/grep/bash use,
    # so a large bundled skill can't flood the turn context — the body is spilled to
    # a file with a preview + read_file pointer. The directory pointer and how-to-read
    # header stay inline so the agent can still navigate even when the body offloads.
    body = offload_if_large(
        read_skill_body(skill), kind="skill", key=str(skill.root),
        workspace_root=ctx.deps.workspace.root,
    )
    return (
        f"Skill directory: {skill.root}\n"
        f"To read a file the skill points at (e.g. ./foo.md), call "
        f"read_skill_file({name!r}, <path-relative-to-skill>); read_file with the "
        f"absolute path under the skill directory also works.\n\n"
        f"{body}"
    )


def read_skill_file(ctx: RunContext[Deps], name: str, path: str) -> str:
    """Read a file bundled inside a skill (e.g. `references/REFERENCE.md`
    or `scripts/run.py`), where `path` is relative to the skill's
    directory. Use after activate_skill when its instructions point you at
    a bundled file. Works for skills in any scope, including global ones
    outside the workspace, and saves you needing the skill's absolute path."""
    skill = find_skill(ctx.deps.workspace.root, name, dirs=ctx.deps.workspace.skill_dirs)
    if skill is None:
        return f"No skill named {name!r}. See the skills index."
    # Same context-flood guard as read_file: a large bundled file is spilled to a
    # file with a preview + read_file pointer instead of being inlined whole.
    return offload_if_large(
        read_bundled_file(skill, path), kind="skill-file", key=f"{skill.root}\0{path}",
        workspace_root=ctx.deps.workspace.root,
    )
