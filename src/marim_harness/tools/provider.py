import asyncio
import functools
import json
import logging
import re
from collections.abc import Iterable
from datetime import datetime, timezone
from typing import Annotated, Literal, Protocol, TypeVar

from pydantic import BeforeValidator
from pydantic_ai import ModelRetry, RunContext

from ..ask_user import Choice, Question, answers_to_json, coerce_questions
from ..jobs import render_jobs
from ..runtime.deps import Deps, HarnessAgent, SubAgent
from ..runtime.permissions import Mode
from ..tasks import Task, summarize
from ..workspace.agents import compose_subagent_task
from ..workspace.memory import global_scope, project_scope, read_memory, save_memory
from ..workspace.plans import write_plan
from ..workspace.skills import discover_skills, find_skill, read_bundled_file, read_skill_body
from . import fetch, fs, shell, web

# Re-exported for backward compatibility; defined in the leaf module ``names``
# so importers (e.g. workspace.agents) don't pull in all of ``provider`` and
# form an import cycle.
from .names import (  # noqa: F401
    GATED_TOOLS,
    LSP_TOOLS,
    NET_TOOLS,
    READ_TOOLS,
    SUBAGENT_MAX_DEPTH,
    SUBAGENT_TOOLS,
)

logger = logging.getLogger(__name__)

_T = TypeVar("_T")


def _decode_json_list(value: object) -> object:
    """Before-validator for an array tool argument: some models serialize a list
    argument as a JSON *string* (e.g. ``'[{"old_string": …}]'``) rather than a
    real array. Decode such a string to the list it represents; pass anything
    else through untouched, so a genuine list validates normally and a non-JSON
    string still surfaces the real validation error instead of being swallowed."""
    if isinstance(value, str):
        try:
            return json.loads(value)
        except (ValueError, TypeError):
            return value
    return value


# A ``list[T]`` tool argument that also tolerates a JSON-stringified list. The
# before-validator runs ahead of list validation; the JSON schema advertised to
# the model stays ``array`` (BeforeValidator leaves it unchanged), so a
# well-behaved model is unaffected while a lenient one doesn't fail the turn on a
# stringified array. Applied to every array-typed tool arg (edits/todos/questions).
LenientList = Annotated[list[_T], BeforeValidator(_decode_json_list)]

# Foreground bash timeout, expressed in milliseconds to match the convention the
# model already uses (Claude Code's Bash tool is ms; models reliably pass ms even
# to a seconds parameter). The value is clamped to ``_BASH_MAX_TIMEOUT_MS`` so a
# mistaken huge value (or a hung command) can't block the turn for hours — past
# that ceiling the model should use ``background=True``. NB: this is enforced
# inside the tool via shell.run_bash, NOT via pydantic-ai's ``agent.tool(timeout=)``:
# that wrapper is a static cap that can't see the per-call argument, silently
# overrides it, and — worse — raises ModelRetry on expiry, which burns the tool's
# retry budget and kills the whole (sub-)agent after two slow commands.
_BASH_DEFAULT_TIMEOUT_MS = 30_000
_BASH_MAX_TIMEOUT_MS = 600_000


def _resolve_bash_timeout_seconds(timeout_ms: int | None) -> int:
    """Clamp a model-supplied foreground timeout (milliseconds) to a sane range and
    return whole seconds for ``shell.run_bash``. ``None`` falls back to the default."""
    ms = _BASH_DEFAULT_TIMEOUT_MS if timeout_ms is None else int(timeout_ms)
    ms = max(1000, min(ms, _BASH_MAX_TIMEOUT_MS))
    return ms // 1000

# Default output budget for auto-detached spawns (≈3k tokens/report) — keeps a
# wide fan-out's synthesis prompt bounded while preserving the full report in the
# spill file. Only applied when the model did not pass an explicit max_output_chars.
_DETACH_OUTPUT_BUDGET = 12000

_ASK_USER_EMPTY = "ask_user needs at least one question, each with at least one option."
_ASK_USER_NO_UI = (
    "Can't ask the user — no interactive UI here. Proceed with your best judgment."
)
_ASK_USER_CANCELLED = "User dismissed the prompt without answering."


# --- tool implementations (module-level so they can be registered onto the main
# agent gated, or onto a sub-agent plain, from a single source of truth) ---


async def fetch_url(
    ctx: RunContext[Deps],
    url: str,
    prompt: str | None = None,
) -> str:
    """Fetch and read content from a specific URL to augment context with live web
    content. Returns the page body as clean Markdown. Accepts a URL (http/https)
    and an optional `prompt` describing what to extract or look for.

    Use this when you need the actual content of a page — web_search only returns
    titles and snippets. HTML pages are converted to Markdown; JSON is
    pretty-printed; plain text is returned as-is. A large page is saved to a file
    under the workspace and you get a handle + preview back — read_file/grep that
    path to page through it — so it doesn't flood context."""
    return await fetch.fetch_url(
        url, prompt=prompt, workspace_root=ctx.deps.workspace.root
    )


def read_file(
    ctx: RunContext[Deps], path: str, offset: int = 1, limit: int | None = None
) -> str:
    """Read a text file. `path` is relative to the workspace root.

    For large files, read a window instead of the whole thing: `offset` is the
    1-based line to start at and `limit` caps the line count. Prefer locating
    what you need first (with `grep`/`tree`) and reading a targeted range — a
    read with no `limit` is capped and will tell you how to page on.

    Skill directories (which may live outside the workspace) are also readable by
    their absolute path, so a skill's bundled files can be read this way too."""
    # Whitelist every discovered skill's directory for reading, so an agent that
    # reaches for a skill's bundled file by absolute path succeeds even when the
    # skill lives outside the workspace (discover_skills is cached per workspace).
    skill_roots = tuple(s.root for s in discover_skills(ctx.deps.workspace.root))
    return fs.read_file(
        ctx.deps.workspace.root, path, offset=offset, limit=limit,
        extra_read_roots=skill_roots, ledger=ctx.deps.reads,
    )


def glob(ctx: RunContext[Deps], pattern: str) -> str:
    """List files matching a glob pattern (e.g. `**/*.py`)."""
    return fs.glob_files(ctx.deps.workspace.root, pattern)


def tree(ctx: RunContext[Deps], path: str = ".", depth: int = 2) -> str:
    """Show a directory tree. `depth=1` lists one level (like ls); higher
    descends further. Noise dirs (.git, node_modules, …) aren't expanded."""
    return fs.tree(ctx.deps.workspace.root, path, depth)


def _grep_int_flag(key: str, val: object) -> int:
    """Coerce a ripgrep context flag (`-A`/`-B`/`-C`) value to a non-negative int,
    raising a model-facing retry on garbage rather than a 500."""
    try:
        return max(0, int(val))  # type: ignore[arg-type]
    except (TypeError, ValueError):
        raise ModelRetry(f"grep: {key} expects an integer, got {val!r}.") from None


def grep(
    ctx: RunContext[Deps],
    pattern: str,
    path: str | None = None,
    glob: str | None = None,
    type: str | None = None,
    output_mode: Literal["content", "files_with_matches", "count"] = "content",
    head_limit: int | None = None,
    multiline: bool = False,
    **flags: object,
) -> str:
    """Search file contents for a regex (ripgrep-style), returning matches in the
    workspace.

    - `path` scopes the search to a file or directory (default: whole workspace).
    - `glob` filters files by name, e.g. `*.py` or `*.{ts,tsx}`.
    - `type` filters by language, e.g. `py`, `js`, `rust`.
    - `output_mode`: `content` (default) shows `path:line:text`;
      `files_with_matches` lists only matching file paths; `count` shows
      `path:count` per file.
    - `head_limit` caps how many output rows come back.
    - `multiline` lets the pattern span lines (`.` matches newlines).
    - `-i` (bool) searches case-insensitively. `-n` is accepted but a no-op:
      line numbers are always included in `content` mode.
    - `-A` / `-B` / `-C` (ints) show that many context lines after / before /
      around each match (`content` mode only).

    Skips noise dirs (.git, node_modules, .venv, …) and binary files; large
    results are offloaded to a file with a preview."""
    case_insensitive = False
    before = after = 0
    for key, val in flags.items():
        if key == "-i":
            case_insensitive = bool(val)
        elif key == "-n":
            pass  # line numbers are always emitted in content mode
        elif key == "-A":
            after = _grep_int_flag(key, val)
        elif key == "-B":
            before = _grep_int_flag(key, val)
        elif key == "-C":
            before = after = _grep_int_flag(key, val)
        else:
            raise ModelRetry(
                f"grep: unknown argument {key!r}. Supported: pattern, path, glob, "
                "type, output_mode, head_limit, multiline, -i, -n, -A, -B, -C."
            )
    return fs.grep(
        ctx.deps.workspace.root,
        pattern,
        path,
        glob=glob,
        file_type=type,
        output_mode=output_mode,
        head_limit=head_limit,
        case_insensitive=case_insensitive,
        before_context=before,
        after_context=after,
        multiline=multiline,
    )


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


async def update_tasks(ctx: RunContext[Deps], todos: LenientList[Task]) -> str:
    """Maintain your checklist for the current multi-step task. Pass the
    FULL list every time — it replaces the previous one. Each item is
    {text, status} where status is pending, in_progress, or done. Keep
    exactly one item in_progress, and mark items done as you finish them.
    Use this for non-trivial work spanning several steps so progress is
    visible; skip it for single-step requests. No approval is needed."""
    before = {t.text: t.status for t in ctx.deps.tasks.items}
    ctx.deps.tasks.replace(todos)
    th = ctx.deps.services.turn_hooks
    if th is not None:
        for t in ctx.deps.tasks.items:
            if t.status == "done" and before.get(t.text) != "done":
                await th.task_completed(task_subject=t.text)
    return summarize(ctx.deps.tasks.items)


async def ask_user(ctx: RunContext[Deps], questions: LenientList[Question]) -> str:
    """Ask the user to choose between concrete options, pausing your turn until
    they answer. Use this only when the user's decision changes what you do next
    and you can't settle it yourself or from the code — not for things you can
    verify or reasonably assume.

    Pass 1–4 questions. Each is {question, header, options, multi}: `header` is a
    short label the answer is returned under; `options` is a list of {label,
    description} choices (description optional); set `multi` true to let the user
    pick several. A free-text field is offered on every question automatically —
    don't add an "other" option yourself.

    Returns a JSON object keyed by each question's `header`: a single-select
    answer is the chosen label (or the user's typed free text); a multi-select
    answer is a list of chosen labels. If there's no interactive UI, or the user
    dismisses the prompt, you get a short note instead — proceed with your best
    judgment."""
    coerced = coerce_questions(questions)
    if not coerced:
        return _ASK_USER_EMPTY
    if ctx.deps.ui.ask_user is None:
        return _ASK_USER_NO_UI
    th = ctx.deps.services.turn_hooks
    if th is not None:
        await th.notification(
            "ask_user", "Question from agent", coerced[0].question
        )
    answers = await ctx.deps.ui.ask_user(coerced)
    if not answers:
        return _ASK_USER_CANCELLED
    return answers_to_json(answers)


_PLAN_CHOICES = [
    Choice("Execute hands-off (auto)", "Run the whole plan without further prompts."),
    Choice("Execute step-by-step (ask)", "Run the plan, approving each change."),
    Choice("Hand off to sub-agent", "Spawn a sub-agent to implement the plan file."),
    Choice("Keep planning", "Save the plan as a draft and keep refining."),
]
_PLAN_EXEC_MODES = {
    "Execute hands-off (auto)": Mode.auto,
    "Execute step-by-step (ask)": Mode.ask,
}


async def present_plan(
    ctx: RunContext[Deps], summary: str, steps: LenientList[str]
) -> str:
    """Present your finished plan and let the user choose how to execute it. Call
    this at the END of a planning turn, once you have researched the task and have
    a concrete, ordered plan.

    `summary` is a short paragraph describing the approach; `steps` is the ordered
    list of concrete steps. The plan is saved to `.marim/plans/`, mirrored into
    your task checklist, and the user is asked whether to execute it hands-off,
    step-by-step, hand it to a sub-agent, or keep planning. If they approve
    execution, the approval mode switches and you should begin carrying out the
    plan starting at step one. If there is no interactive UI, the plan is saved
    and you stay in plan mode."""
    clean = [s.strip() for s in (steps or []) if s and s.strip()]
    if not clean:
        raise ModelRetry("present_plan needs at least one concrete step.")

    created = datetime.now(timezone.utc).isoformat(timespec="seconds")
    # Stamp the plan with the active session id so filenames are stable per session
    # (one file per plan per session, re-presenting overwrites). The getter is wired
    # by the Harness; it is None in headless/tests, where we fall back to the
    # workspace root name so the slug is still stable and non-empty.
    get_sid = ctx.deps.services.get_session_id
    sid = get_sid() if get_sid is not None else None
    session_id = sid or ctx.deps.workspace.root.name or "session"
    try:
        path = write_plan(
            ctx.deps.workspace.root,
            session_id=session_id,
            summary=summary,
            steps=clean,
            created=created,
        )
    except OSError:
        logger.warning("failed to write plan artifact", exc_info=True)
        path = None

    ctx.deps.tasks.replace([Task(text=s) for s in clean])

    if ctx.deps.ui.ask_user is None:
        return (
            f"Plan saved{f' to {path}' if path else ''}. No interactive UI, so "
            "staying in plan mode — share the plan and await direction."
        )

    answers = await ctx.deps.ui.ask_user(
        [Question(question="How should I execute this plan?", header="execution",
                  options=_PLAN_CHOICES)]
    )
    choice = (answers or {}).get("execution", "Keep planning")

    new_mode = _PLAN_EXEC_MODES.get(choice if isinstance(choice, str) else "")
    if new_mode is not None:
        # Tools hold only ctx.deps (not the Harness), so set mode directly; the
        # on_mode_change hook below performs the UI refresh that Harness.set_mode
        # would otherwise trigger.
        ctx.deps.workspace.mode = new_mode
        if ctx.deps.ui.on_mode_change is not None:
            ctx.deps.ui.on_mode_change()
        return (
            f"Plan approved. Approval mode is now {new_mode.value}. Begin executing "
            "the plan now, starting with step one."
        )
    if choice == "Hand off to sub-agent" and path is not None:
        return (
            f"Plan saved to {path}. To execute, call spawn_agent (type 'general') "
            f"with instructions to implement the steps in {path} in order, then "
            "report back. You remain in plan mode meanwhile."
        )
    return (
        f"Plan saved{f' to {path}' if path else ''} as a draft. Still in plan mode "
        "— refine it and call present_plan again when ready."
    )


async def web_search(
    ctx: RunContext[Deps],
    query: str,
    categories: str | None = None,
    max_results: int = 10,
) -> str:
    """Search the web via a self-hosted SearXNG instance and return formatted results.

    *query* is the search string.  *categories* restricts results to a SearXNG
    category (e.g. "general", "images", "news", "science").  *max_results*
    caps how many hits are returned (default 10, max 50)."""
    return await web.web_search(query, categories=categories, max_results=max_results)


def _coerce_mcp(mcp: "list[str] | str | None") -> list[str] | None:
    """Normalize the `mcp` grant into a list of server names, or None.

    Weaker models often serialize the array argument as a JSON string
    (``'["mddocs"]'``) or a comma-separated string (``'mddocs, sentry'``)
    rather than a real array. Accepting those forms keeps a mis-encoding from
    failing the whole turn on schema validation. Returns None for an empty
    grant so it flows through as "no MCP access"."""
    if mcp is None:
        return None
    if isinstance(mcp, str):
        text = mcp.strip()
        if not text:
            return None
        try:
            parsed = json.loads(text)
        except ValueError:
            parsed = None
        if isinstance(parsed, list):
            items: Iterable = parsed
        elif isinstance(parsed, str):
            items = [parsed]
        else:  # not JSON (or a number/object) — treat as comma-separated
            items = text.split(",")
    else:
        items = mcp
    names = [str(item).strip() for item in items]
    cleaned = [name for name in names if name]
    return cleaned or None


def _detach_handoff(job_id: str) -> str:
    """The return for an auto-detached spawn: tell the agent it's running in the
    background and that it may end its turn (wake will deliver the report) or wait."""
    return (
        f"Started detached sub-agent {job_id}, running in the background. "
        f"End your turn to let it run — its report will be "
        f"delivered to you when it finishes — or wait_for_job(\"{job_id}\") if you "
        f"need the result in this turn. For a fan-out, ending the turn is better."
    )


async def spawn_agent(
    ctx: RunContext[Deps],
    type: str,
    task: str,
    description: str | None = None,
    background: bool | None = None,
    mcp: list[str] | str | None = None,
    max_output_chars: int | None = None,
    returns: str | None = None,
    constraints: str | None = None,
    context: str | None = None,
    model: str | None = None,
    isolation: str | None = None,
    max_depth: int | None = None,
) -> str:
    """Delegate a sub-task to an isolated sub-agent that runs on the same model
    and reports back. `type` is a built-in — `explore` (read-only investigation;
    reports findings, changes nothing) or `general` (full toolset; carries out a
    focused sub-task autonomously) — or a custom agent by name, as listed in the
    sub-agents index. The sub-agent starts with a clean context, does `task`, and
    its final message becomes this tool's result. Spawn several in one turn to
    fan out independent work. Sub-agents can spawn deeper sub-agents, but are
    limited by a maximum nesting depth — attempts to spawn beyond that limit are
    refused.

    Leave `background` unset for a normal spawn or fan-out — that is almost always
    right. When detached-fanout mode is on, an unset spawn auto-detaches: it shows a
    live sub-agent card, returns a job handle, and you either end your turn (its
    report is delivered when it finishes) or wait_for_job for it inline. Only set
    `background=True` for a genuine fire-and-forget job; you do NOT need it to run a
    fan-out in parallel — unset already does, with better display. `background=False`
    forces an inline run (no detach).

    `mcp` grants the sub-agent specific MCP servers by name (none by default).
    Pass the names listed as enabled in the sub-agents index — e.g.
    `mcp=["mddocs"]` lets the sub-agent use that server's tools, gated the same
    way your own MCP calls are. Unknown or disabled names are ignored and noted
    in the report.

    `max_output_chars` caps the report this spawn returns into your context — set
    it when you're fanning out and want bounded inflow. It's a budget the
    sub-agent distills toward (it's told to lead with the conclusion and
    summarize to fit), not a blind truncation. It's also enforced losslessly, for
    both foreground and background spawns: a report over budget is written to a
    workspace file and replaced with a within-budget head + a pointer to that
    file, so nothing is lost — you can read the file if you need the detail. Leave
    it unset for an unbounded report.

    `returns`, `constraints`, and `context` are optional structured fields folded
    into the sub-agent's prompt — all freeform text, all additive (omit any and
    nothing changes). Use them to give a clean-context sub-agent what it can't
    infer: `returns` is the output contract (what to hand back and in what shape —
    the highest-leverage field, since otherwise you get a shape you have to re-ask
    for); `constraints` are boundaries on how to work (a soft nudge — real tool
    reach is still set by `type`/`mcp`, not prose); `context` is the orchestration-
    level background it can't see (why this task, what's already known). The plain
    `task` stays the one required ask.

    `description` is an optional short (3-5 word) label for this spawn — it does
    not affect what the sub-agent does, only how the spawn is shown (it titles the
    sub-agent card and the tool line). Omit it and the card falls back to `task`.

    `model` optionally runs this spawn on a different model than yours — pass a
    cheaper model for read-only fan-out, or a stronger one for a hard sub-task.
    Omit it to inherit your current model (the usual case). For a sub-agent whose
    definition sets `backend: claude-cli`, `model` is a Claude Code model name (an
    alias like `opus`, `sonnet`, `haiku`, or `fable`, or a full id like
    `claude-sonnet-4-6`) passed straight to the CLI — not a harness/OpenRouter
    model id. Omit it to use the agent's own `model:` default or the CLI's
    configured default.

    `isolation="worktree"` runs a mutating spawn in its own git worktree, so
    several spawns editing files at once can't clobber each other or your working
    tree. Its changes are committed to a branch (named in the report) and the
    worktree is removed — merge or review the branch afterward. The worktree
    branches from the last commit, so it won't see uncommitted changes in your
    tree. Only needed when spawns write in parallel; omit for read-only work.

    `max_depth` is the depth ceiling for nested spawning. The main agent starts
    at depth 0. Each spawn increments depth by 1. When `depth + 1 >= max_depth`,
    the tool refuses. This parameter is pre-filled by the harness — callers should
    omit it."""
    mcp_names = _coerce_mcp(mcp)
    # Depth enforcement: refuse spawns that would exceed the depth ceiling.
    # max_depth is None for the main agent (defaults to SUBAGENT_MAX_DEPTH).
    # Sub-agents get it bound via functools.partial by SubagentRunner.build().
    from .names import SUBAGENT_MAX_DEPTH
    effective_max = max_depth if max_depth is not None else SUBAGENT_MAX_DEPTH
    if ctx.deps.subagent_depth + 1 >= effective_max:
        return (
            f"Cannot spawn sub-agent: already at depth "
            f"{ctx.deps.subagent_depth}, max depth is {effective_max}."
        )
    # Background spawning is main-agent-only: a sub-agent's turn ends before its
    # background child finishes, so the child's report would always be orphaned
    # (owned by the job registry, never seen by the spawner). Sub-agents should
    # fan out foreground children instead — results return to the caller.
    if background and ctx.deps.subagent_depth > 0:
        return (
            "Background spawning is only available to the top-level agent. "
            "Spawn this child in the foreground, or have the main agent "
            "spawn it as a background job with background=True."
        )
    task = compose_subagent_task(
        task, returns=returns, constraints=constraints, context=context
    )
    auto_detached = (
        background is None and ctx.deps.ui.detach_fanout and ctx.deps.ui.interactive
    )
    if background or auto_detached:
        if ctx.deps.services.run_background_agent is None:
            return "Background sub-agents are not available in this context."
        # For auto-detached spawns, default to _DETACH_OUTPUT_BUDGET when the
        # model did not pass an explicit cap — keeps the synthesis prompt bounded
        # across a wide fan-out while the full report is preserved in the spill file.
        if auto_detached and max_output_chars is None:
            budget = _DETACH_OUTPUT_BUDGET
        else:
            budget = max_output_chars
        # Prefer the short `description` for the job label (the jobs panel and the
        # wait row read it) — the composed `task` is a full multi-section prompt.
        label = f"{type}: {description or task}"
        job_id = ctx.deps.jobs.register(
            "agent", label,
            ctx.deps.services.run_background_agent(
                type, task, mcp_names, budget, model, isolation,
                ctx.tool_call_id or "", ctx.deps.subagent_depth,
            ),
        )
        if auto_detached:
            return _detach_handoff(job_id)
        return f"Started {job_id} (agent) — {label[:60]}"
    if ctx.deps.services.run_subagent is None:
        return "Sub-agents are not available in this context."
    # Pass the *caller's* depth so the runner builds the child at caller_depth + 1.
    # The runner can't read this off its own deps — those are fixed at the main
    # agent's depth (0), so a depth-1 sub-agent's spawn would otherwise be mis-sized.
    return await ctx.deps.services.run_subagent(
        type, task, ctx.tool_call_id or "", mcp_names, max_output_chars, model,
        isolation, ctx.deps.subagent_depth,
    )


# A real diagnostics report is one or more "path:line:col: severity: message"
# lines (see lsp.diagnostics.format_diagnostics). The manager's clean /
# unavailable / disabled responses never take that shape, so detect actual
# diagnostics structurally — a filename or message containing a word like
# "disabled" must not suppress real errors.
_DIAGNOSTIC_LINE = re.compile(r":\d+:\d+: (?:error|warning|info|hint): ")


async def _with_diagnostics(ctx: RunContext[Deps], path: str, result: str) -> str:
    """Append best-effort LSP diagnostics for ``path`` to a write/edit ``result``.

    No-op when no LSP is wired, when the language isn't served, or when the file
    is clean — so a successful edit only grows output when there's something the
    model should fix. Never raises: any failure leaves ``result`` untouched,
    and is logged at DEBUG so a broken LSP setup isn't indistinguishable from a
    clean file."""
    logger = logging.getLogger(__name__)
    if ctx.deps.services.lsp is None:
        return result
    try:
        report = await ctx.deps.services.lsp.diagnostics(path, settle=0.8)
    except Exception as exc:  # noqa: BLE001 — diagnostics must never fail an edit
        logger.debug("diagnostics fetch failed for %s: %s", path, exc)
        return result
    if not report or not _DIAGNOSTIC_LINE.search(report):
        return result
    return f"{result}\n\ndiagnostics:\n{report}"


async def write_file(ctx: RunContext[Deps], path: str, content: str) -> str:
    """Create or overwrite a file. `path` is relative to the workspace root."""
    # This is ``async def`` so it can ``await _with_diagnostics`` — but that signature
    # opts out of pydantic-ai's auto thread-offload (it awaits async tools directly on
    # the event loop). ``fs.write_file`` does a blocking read + atomic write + double
    # fsync, so run it in a worker thread to keep the loop free for other tool calls.
    result = await asyncio.to_thread(
        fs.write_file, ctx.deps.workspace.root, path, content, ctx.deps.reads
    )
    return await _with_diagnostics(ctx, path, result)


async def edit_file(ctx: RunContext[Deps], path: str, edits: LenientList[fs.Edit]) -> str:
    """Apply one or more find/replace edits to a file, in order and
    all-or-nothing. Each edit is {old_string, new_string, replace_all?};
    old_string must match exactly once unless replace_all is set."""
    # Offload the blocking fs work to a thread (see ``write_file`` above): the async
    # signature exists only to ``await _with_diagnostics``, and would otherwise run the
    # read + atomic write + fsyncs directly on the event loop.
    result = await asyncio.to_thread(
        fs.edit_file, ctx.deps.workspace.root, path, edits, ctx.deps.reads
    )
    return await _with_diagnostics(ctx, path, result)


async def bash(
    ctx: RunContext[Deps],
    command: str,
    description: str = "",
    background: bool = False,
    timeout: int | None = None,
) -> str:
    """Run a shell command in the workspace root.

    `description` is an optional one-line summary of what the command does, in
    active voice (e.g. "Count total source lines"); it's shown in the UI and
    session history and is otherwise ignored — it never affects execution.

    `timeout` caps a foreground run, in milliseconds (default 30000, max 600000);
    it is a total wall-clock ceiling, so a slow-but-chatty command still stops at
    the limit. Raise it for a command you expect to be slow (a big test run)
    rather than reaching for `background`. It is ignored for background runs,
    which are detached and never time out.

    Set `background=True` for long-running commands (dev servers, builds, test
    watchers): the command is launched detached and the tool returns immediately
    with a job id instead of blocking. Check on it later with job_output /
    wait_for_job, or stop it with cancel_job. A foreground run (the default) waits
    for the command and is subject to a timeout, so use background for anything
    that won't finish promptly."""
    reason = ctx.deps.workspace.command_policy.check(command)
    if reason is not None:
        return f"Blocked by command policy: {reason}"
    if background:
        bp = await shell.start_bash(ctx.deps.workspace.root, command)
        job_id = ctx.deps.jobs.register(
            "bash", command, bp.wait(), kill=bp.kill, output_fn=bp.output
        )
        return f"Started {job_id} (bash) — {command[:60]}"
    timeout_s = _resolve_bash_timeout_seconds(timeout)
    return await shell.run_bash(ctx.deps.workspace.root, command, timeout=timeout_s)


def jobs(ctx: RunContext[Deps]) -> str:
    """List the background jobs you've launched this session, with their id, kind
    (bash/agent), label, and status (running/done/failed/cancelled). Use this to
    see what's still in flight before pulling results with job_output or
    wait_for_job."""
    rows = render_jobs(ctx.deps.jobs.list())
    return rows or "No background jobs."


def job_output(ctx: RunContext[Deps], id: str) -> str:
    """Read a background job's output by id without blocking: the final result if
    it's finished, the live output so far for a running bash job, or a running
    marker otherwise. To block until a job finishes, use wait_for_job instead."""
    return ctx.deps.jobs.output(id, mark_seen=True)


async def wait_for_job(ctx: RunContext[Deps], id: str, timeout: float = 60) -> str:
    """Block until a background job finishes (up to `timeout` seconds — note this
    one is seconds, unlike bash's millisecond timeout), then
    return its result. If it's still running when the timeout elapses, the job
    keeps going and you get a "still running" note — call again or check
    job_output later. Use this when you need a job's result before continuing.
    To make progress meanwhile, emit independent read_file/grep calls in the SAME
    response as this wait — they run concurrently while the job finishes."""
    return await ctx.deps.jobs.wait(id, timeout)


async def cancel_job(ctx: RunContext[Deps], id: str) -> str:
    """Stop a running background job by id: kills its process (bash) or cancels
    its run (agent). Finished jobs are left as-is."""
    return await ctx.deps.jobs.cancel(id)


async def job(
    ctx: RunContext[Deps],
    action: Literal["list", "output", "wait", "cancel"],
    id: str = "",
    timeout: float = 60,
) -> str:
    """Manage background jobs you've launched this session. `action`:
    - "list": show every job with its id, kind (bash/agent), label, and status.
    - "output": read job `id`'s output without blocking — final result if done,
      live output so far for a running bash job.
    - "wait": block until job `id` finishes (up to `timeout` seconds) and return
      its result; a still-running note if the timeout elapses (the job keeps going).
    - "cancel": stop running job `id` (kills its process or cancels its run).
    `id` is required for every action except "list"; `timeout` applies only to
    "wait" and is in seconds (unlike bash's millisecond timeout)."""
    if action == "list":
        return render_jobs(ctx.deps.jobs.list()) or "No background jobs."
    if not id:
        return f"job: action {action!r} needs an id (use action=\"list\" to find it)."
    if action == "output":
        return ctx.deps.jobs.output(id, mark_seen=True)
    if action == "wait":
        return await ctx.deps.jobs.wait(id, timeout)
    return await ctx.deps.jobs.cancel(id)  # action == "cancel"


# Name -> implementation for the tools a sub-agent may receive. The Harness
# decides which names to grant; register_subagent registers exactly those.
_SUBAGENT_FNS = {
    "read_file": read_file,
    "glob": glob,
    "tree": tree,
    "grep": grep,
    "goto_definition": goto_definition,
    "find_references": find_references,
    "hover": hover,
    "document_symbols": document_symbols,
    "workspace_symbols": workspace_symbols,
    "diagnostics": diagnostics,
    "web_search": web_search,
    "fetch_url": fetch_url,
    "write_file": write_file,
    "edit_file": edit_file,
    "bash": bash,
}


class ToolProvider(Protocol):
    """Registers a set of tools onto an Agent. The swap point for future
    pydantic-ai-harness FileSystem/Shell capabilities."""

    def register(self, agent: HarnessAgent) -> None:
        ...

    def register_subagent(self, agent: SubAgent, tool_names: Iterable[str]) -> None:
        ...


class BuiltinToolProvider:
    """Hand-written fs + shell tools backed by the pure functions in this package."""

    def __init__(self, *, register_lsp_tools: bool = True,
                 combined_job_tool: bool = False) -> None:
        """``register_lsp_tools`` gates the six LSP navigation tools for both the
        main agent and sub-agents. The harness derives it from the LSP config
        (``lsp_enabled and lsp_tools_enabled``); diagnostics-on-edit is wired
        separately through ``deps.services.lsp`` and is unaffected by this flag.

        ``combined_job_tool`` (prototype) swaps the four job tools
        (jobs/job_output/wait_for_job/cancel_job) for a single ``job(action, …)``
        tool. Job tools are main-agent only, so this affects ``register`` only."""
        self._register_lsp_tools = register_lsp_tools
        self._combined_job_tool = combined_job_tool

    def register(self, agent: HarnessAgent) -> None:
        """Register the full main-agent toolset: read tools, the memory / skill /
        task / spawn tools, and the workspace-mutating tools behind approval."""
        # Registered individually rather than via a loop: each tool has a distinct
        # signature, and a loop variable unions them into a type the .tool()
        # overloads can't resolve.
        agent.tool(read_file)
        agent.tool(glob)
        agent.tool(tree)
        agent.tool(grep)
        if self._register_lsp_tools:
            agent.tool(goto_definition)
            agent.tool(find_references)
            agent.tool(hover)
            agent.tool(document_symbols)
            agent.tool(workspace_symbols)
            agent.tool(diagnostics)
        agent.tool(web_search)
        agent.tool(fetch_url)
        agent.tool(remember)
        agent.tool(recall)
        agent.tool(activate_skill)
        agent.tool(read_skill_file)
        agent.tool(update_tasks)
        agent.tool(ask_user)
        agent.tool(present_plan)
        bound_spawn = functools.partial(spawn_agent, max_depth=SUBAGENT_MAX_DEPTH)
        # functools.partial accepts arbitrary attributes at runtime, but its type
        # stub doesn't declare __name__/__qualname__ — hence the ignores.
        bound_spawn.__name__ = "spawn_agent"  # type: ignore[attr-defined]
        bound_spawn.__qualname__ = "spawn_agent"  # type: ignore[attr-defined]
        agent.tool(bound_spawn)
        if self._combined_job_tool:
            agent.tool(job)
        else:
            agent.tool(jobs)
            agent.tool(job_output)
            agent.tool(wait_for_job)
            agent.tool(cancel_job)
        agent.tool(requires_approval=True)(write_file)
        agent.tool(requires_approval=True)(edit_file)
        agent.tool(requires_approval=True)(bash)

    def register_subagent(self, agent: SubAgent, tool_names: Iterable[str]) -> None:
        """Register exactly ``tool_names`` onto a sub-agent. Gated tools are
        registered *plain* (no approval round) — reach is decided up front by
        which names the Harness grants, not by prompting mid-run. spawn_agent is
        never among them, so sub-agents can't recurse."""
        for name in sorted(set(tool_names)):
            if not self._register_lsp_tools and name in LSP_TOOLS:
                continue
            fn = _SUBAGENT_FNS.get(name)
            if fn is None:
                continue
            agent.tool(fn)
