from pydantic_ai import RunContext

from ..runtime.deps import Deps
from ..workspace.skills import find_skill, read_bundled_file, read_skill_body


def activate_skill(ctx: RunContext[Deps], name: str) -> str:
    """Load a skill's full instructions by `name`, as listed in the
    skills index. Returns the SKILL.md body plus the skill's absolute
    directory, so you can read its bundled files with read_skill_file and
    run any scripts with bash using that absolute path. Activate a skill
    when the task matches its one-line description, then follow what it
    says."""
    skill = find_skill(ctx.deps.workspace.root, name)
    if skill is None:
        return f"No skill named {name!r}. See the skills index."
    return (
        f"Skill directory: {skill.root}\n"
        f"To read a file the skill points at (e.g. ./foo.md), call "
        f"read_skill_file({name!r}, <path-relative-to-skill>); read_file with the "
        f"absolute path under the skill directory also works.\n\n"
        f"{read_skill_body(skill)}"
    )


def read_skill_file(ctx: RunContext[Deps], name: str, path: str) -> str:
    """Read a file bundled inside a skill (e.g. `references/REFERENCE.md`
    or `scripts/run.py`), where `path` is relative to the skill's
    directory. Use after activate_skill when its instructions point you at
    a bundled file. Works for skills in any scope, including global ones
    outside the workspace, and saves you needing the skill's absolute path."""
    skill = find_skill(ctx.deps.workspace.root, name)
    if skill is None:
        return f"No skill named {name!r}. See the skills index."
    return read_bundled_file(skill, path)
