import asyncio
import json
import logging
import re
from collections.abc import Awaitable, Callable, Iterable
from datetime import datetime, timezone
from typing import Annotated, Literal, Protocol, TypeVar

from pydantic import BeforeValidator
from pydantic_ai import ModelRetry, RunContext

from ..ask_user import Choice, Question, answers_to_json, coerce_questions
from ..jobs import JobRegistry, PrerequisiteFailed, _one_line, render_jobs
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


def _decode_json(value: object) -> object:
    """Before-validator for a structured tool argument: some models serialize a
    list or object argument as a JSON *string* (e.g. ``'[{"old_string": …}]'`` or
    ``'{"text": …}'``) rather than a real array/object. Decode such a string to the
    value it represents; pass anything else through untouched, so a genuine
    array/object validates normally and a non-JSON string still surfaces the real
    validation error instead of being swallowed."""
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
LenientList = Annotated[list[_T], BeforeValidator(_decode_json)]

# A single tool argument (or list element) that tolerates a JSON-stringified
# object. Same relax-don't-mask contract as ``LenientList``; used on the object
# element types so a model that stringifies each element (not just the whole list)
# still validates. The advertised schema is unchanged.
Lenient = Annotated[_T, BeforeValidator(_decode_json)]

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


async def update_tasks(ctx: RunContext[Deps], todos: LenientList[Lenient[Task]]) -> str:
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


async def ask_user(ctx: RunContext[Deps], questions: LenientList[Lenient[Question]]) -> str:
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


def _coerce_names(mcp: "list[str] | str | None") -> list[str] | None:
    """Normalize a name-list argument (mcp grant, after ids) into a list, or None.

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


async def _run_after(
    jobs: "JobRegistry",
    after_ids: list[str],
    task: str,
    start_inner: "Callable[[str], Awaitable[str]]",
    state: dict,
) -> str:
    """Body of a dependent background job: wait for prerequisites, fail fast if
    any didn't succeed, then run the real sub-agent with their reports appended
    to its task.

    ``start_inner`` creates the inner run_background_agent coroutine *lazily* —
    the prompt can't be finalized until the prerequisites' reports exist, and an
    eagerly-created coroutine would leak un-awaited on a cancel-before-start
    (the same concern JobRegistry.register's docstring guards). ``state`` is
    shared with the job's output_fn so the jobs panel can show the waiting
    phase without a new job status."""
    settled = await jobs.await_settled(after_ids)
    bad = next((j for j in settled if j.status != "done"), None)
    if bad is not None:
        tail = " ".join((bad.result or "").split())[-160:]
        raise PrerequisiteFailed(
            f"prerequisite {bad.id} {bad.status}" + (f" — {tail}" if tail else "")
        )
    # Clip the heading to one line: a background spawn's label falls back to
    # the full composed (multi-section) task when `description` was omitted,
    # so without _one_line a dependent would receive its prerequisite's entire
    # prompt embedded inside its own "### job-N — ..." heading.
    sections = [
        f"### {j.id} — {_one_line(j.label)}\n{j.result or '(no output)'}" for j in settled
    ]
    full_task = task + "\n\n## Results of prerequisite jobs\n\n" + "\n\n".join(sections)
    state["waiting"] = False
    return await start_inner(full_task)


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
    after: "list[str] | str | None" = None,
    max_output_chars: int | None = None,
    returns: str | None = None,
    constraints: str | None = None,
    context: str | None = None,
    model: str | None = None,
    isolation: str | None = None,
) -> str:
    """Delegate a sub-task to an isolated sub-agent that runs on the same model
    and reports back. `type` is a built-in — `explore` (read-only investigation;
    reports findings, changes nothing — use it to investigate before acting,
    especially over large files/logs/output you don't want cluttering your own
    context) or `general` (full toolset; carries out a focused sub-task
    autonomously) — or a custom agent by name, as listed in the sub-agents index.
    The sub-agent starts with a clean context, does `task`, and
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

    `after` names background job ids (earlier detached spawns or bash jobs) that
    must finish before this spawn starts — use it to chain dependent work, e.g. a
    merge step after the jobs producing its inputs. It requires a detached spawn
    (`background=True`, or auto-detach). The prerequisites' final reports are
    appended to this sub-agent's task under "Results of prerequisite jobs"; size
    them with `max_output_chars` on the *prerequisite* spawns — injection never
    truncates. If a prerequisite fails or is cancelled, this job fails without
    starting (zero tokens spent) and the failure surfaces in the jobs digest.
    Prerequisite ids come from the spawn handoffs ("Started job-N …"); issue a
    dependent spawn in a later response, after those return — ids cannot be
    guessed.

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
    tree. Only needed when spawns write in parallel; omit for read-only work."""
    mcp_names = _coerce_names(mcp)
    after_ids = _coerce_names(after)
    if after_ids is not None:
        # Dedupe while preserving order: a model that lists the same
        # prerequisite id twice (e.g. after=[a, a]) would otherwise inject
        # that prerequisite's report twice into the dependent's task.
        after_ids = list(dict.fromkeys(after_ids))
    # Depth enforcement: refuse spawns that would exceed the depth ceiling. The
    # ceiling rides on Deps (SubagentRunner stamps its configured value into a
    # child's deps) rather than being a tool parameter — a parameter would sit
    # in the advertised schema, where the model could override it and raise its
    # own ceiling.
    effective_max = ctx.deps.subagent_max_depth
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
    # Auto-detach (detached fan-out) is top-level-only, for the same reason the
    # explicit-background guard above is: a sub-agent's turn ends before a
    # detached child finishes, so the child's report — owned by the job registry —
    # would never reach the spawner. A depth>0 spawn with `background` unset runs
    # inline instead.
    auto_detached = (
        background is None
        and ctx.deps.subagent_depth == 0
        and ctx.deps.ui.detach_fanout
        and ctx.deps.ui.interactive
    )
    if after_ids is not None:
        unknown = [jid for jid in after_ids if ctx.deps.jobs.get(jid) is None]
        if unknown:
            return (
                f"Cannot spawn with after={unknown}: no such job(s). "
                "after only accepts ids of already-started background jobs "
                "(see the jobs panel or the digest for valid ids)."
            )
        if not (background or auto_detached):
            return (
                "after= requires a detached spawn. Pass background=True (top-level "
                "agent only), or drop after and wait_for_job the prerequisite "
                "before a foreground spawn."
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
        if after_ids:
            state = {"waiting": True}
            waiting_note = f"(waiting on {', '.join(after_ids)})"
            # Type guard: we've already checked run_background_agent is not None
            # in the guard above, so this is safe.
            run_bg = ctx.deps.services.run_background_agent
            assert run_bg is not None

            def _waiting_output() -> str:
                return waiting_note if state["waiting"] else "(still running)"

            def _start_inner(full_task: str) -> "Awaitable[str]":
                return run_bg(
                    type, full_task, mcp_names, budget, model, isolation,
                    ctx.tool_call_id or "", ctx.deps.subagent_depth,
                )

            job_id = ctx.deps.jobs.register(
                "agent", label,
                _run_after(ctx.deps.jobs, after_ids, task, _start_inner, state),
                output_fn=_waiting_output,
                stream_id=ctx.tool_call_id or None,
            )
        else:
            job_id = ctx.deps.jobs.register(
                "agent", label,
                ctx.deps.services.run_background_agent(
                    type, task, mcp_names, budget, model, isolation,
                    ctx.tool_call_id or "", ctx.deps.subagent_depth,
                ),
                stream_id=ctx.tool_call_id or None,
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
    that won't finish promptly. Background runs are top-level-agent only:
    sub-agents run everything in the foreground (raise `timeout` for slow
    commands instead)."""
    reason = ctx.deps.workspace.command_policy.check(command)
    if reason is not None:
        return f"Blocked by command policy: {reason}"
    # Background jobs are main-agent-only, like background spawns: sub-agents
    # have no job tools (job_output/wait_for_job/cancel_job are not in
    # SUBAGENT_TOOLS) and no wake loop, so a job they started would be
    # unretrievable by them — its completion digest would land on the main
    # agent, who never asked for it.
    if background and ctx.deps.subagent_depth > 0:
        return (
            "Background commands are only available to the top-level agent. "
            "Run this in the foreground instead — raise `timeout` if it is "
            "slow — or report back and let the main agent start it."
        )
    if background:
        bp = await shell.start_bash(ctx.deps.workspace.root, command)
        job_id = ctx.deps.jobs.register(
            "bash", command, bp.wait(), kill=bp.kill, output_fn=bp.output
        )
        return f"Started {job_id} (bash) — {command[:60]}"
    timeout_s = _resolve_bash_timeout_seconds(timeout)
    return await shell.run_bash(ctx.deps.workspace.root, command, timeout=timeout_s)


_POLL_WAKE_NOTE = "(running jobs wake you on completion — no need to check again)"
_POLL_WARN = (
    "⚠ No change since your last check. If you have no other work, end your "
    "turn — finished jobs wake you and deliver their reports automatically."
)
_POLL_WARN_HEADLESS = (
    "⚠ No change since your last check. Use wait_for_job(id) to block until a "
    "job finishes instead of polling."
)


def _guarded_poll_response(
    ctx: RunContext[Deps], key: str, body: str, *, any_running: bool
) -> str:
    """Apply the poll guard (spec 2026-07-02-job-poll-guard-design) to one
    read-only jobs response. Counts only while something still runs — reading
    settled results is never polling. Interactive sessions escalate: the 2nd
    identical look appends a warning, the 3rd+ replaces the body entirely (a
    wake loop exists, so ending the turn is always safe, and a fresh-looking
    table makes the warning read as boilerplate). Headless has no wake loop and
    may still need the data: append-only, pointing at wait_for_job."""
    if not any_running:
        return body
    count = ctx.deps.jobs.note_poll(key, body)
    if count < 2:
        return body
    if not ctx.deps.ui.interactive:
        return f"{body}\n\n{_POLL_WARN_HEADLESS}"
    if count == 2:
        return f"{body}\n\n{_POLL_WARN}"
    return (
        f"No change since your last check (poll {count}). Stop polling: end "
        "your turn now — finished jobs wake you and deliver their reports "
        "automatically. Use wait_for_job(id) only if you must block on a "
        "result inside this turn."
    )


def _jobs_listing(ctx: RunContext[Deps]) -> str:
    """The shared body of jobs() and job("list"): the rendered table, with the
    standing wake note while anything runs (interactive only — headless has no
    wake loop), passed through the poll guard. render_jobs output is a stable
    projection (no elapsed times), so it doubles as the poll snapshot."""
    listed = ctx.deps.jobs.list()
    rows = render_jobs(listed)
    if not rows:
        return "No background jobs."
    any_running = any(j.status == "running" for j in listed)
    if any_running and ctx.deps.ui.interactive:
        rows = f"{rows}\n{_POLL_WAKE_NOTE}"
    return _guarded_poll_response(ctx, "list", rows, any_running=any_running)


def _job_output_read(ctx: RunContext[Deps], id: str) -> str:
    """The shared body of job_output() and job("output"): the read, passed
    through the poll guard keyed per job while that job still runs. A growing
    bash buffer changes the snapshot every call, so real progress is never
    nagged — only zero-information repeats are."""
    target = ctx.deps.jobs.get(id)
    body = ctx.deps.jobs.output(id, mark_seen=True)
    running = target is not None and target.status == "running"
    return _guarded_poll_response(ctx, f"output:{id}", body, any_running=running)


_WAIT_TIMEOUT_NUDGE = (
    "If you don't need its result to continue this turn, end your turn — the "
    "harness wakes you when it finishes and delivers its report. Wait again "
    "only if you must block on it now."
)


async def _job_wait(ctx: RunContext[Deps], id: str, timeout: float) -> str:
    """The shared body of wait_for_job() and job("wait"). A timed-out wait is
    detected by the job still being in "running" state after the wait returns
    (the registry's message stays opaque here); interactive sessions get an
    end-your-turn nudge appended because the wake loop makes that the cheaper
    move, while headless — which has no wake loop and re-waiting IS the right
    call — keeps the bare note. Softer than the poll guard on purpose: a
    timed-out wait sometimes precedes a legitimate re-wait mid-task."""
    body = await ctx.deps.jobs.wait(id, timeout)
    target = ctx.deps.jobs.get(id)
    timed_out = target is not None and target.status == "running"
    if timed_out and ctx.deps.ui.interactive:
        return f"{body}\n\n{_WAIT_TIMEOUT_NUDGE}"
    return body


def jobs(ctx: RunContext[Deps]) -> str:
    """List the background jobs you've launched this session, with their id, kind
    (bash/agent), label, and status (running/done/failed/cancelled). Use this to
    see what's still in flight before pulling results with job_output or
    wait_for_job. Never call this in a loop to wait — if you have no other work,
    end your turn; the harness wakes you when a job finishes and delivers its
    report."""
    return _jobs_listing(ctx)


def job_output(ctx: RunContext[Deps], id: str) -> str:
    """Read a background job's output by id without blocking: the final result if
    it's finished, the live output so far for a running bash job, or a running
    marker otherwise. To block until a job finishes, use wait_for_job instead."""
    return _job_output_read(ctx, id)


async def wait_for_job(ctx: RunContext[Deps], id: str, timeout: float = 60) -> str:
    """Block until a background job finishes (up to `timeout` seconds — note this
    one is seconds, unlike bash's millisecond timeout), then
    return its result. If it's still running when the timeout elapses, the job
    keeps going and you get a "still running" note — if you don't need the
    result to continue this turn, end your turn instead of re-waiting; the
    report is delivered when it finishes. Use this only when you must block on
    a job's result before continuing. To make progress meanwhile, emit
    independent read_file/grep calls in the SAME response as this wait — they
    run concurrently while the job finishes."""
    return await _job_wait(ctx, id, timeout)


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
      its result; a still-running note if the timeout elapses (the job keeps
      going — if you don't need the result this turn, end your turn instead of
      re-waiting; the report is delivered when it finishes).
    - "cancel": stop running job `id` (kills its process or cancels its run).
    `id` is required for every action except "list"; `timeout` applies only to
    "wait" and is in seconds (unlike bash's millisecond timeout). Never call
    "list" or "output" in a loop to wait — if you have no other work, end your
    turn; the harness wakes you when a job finishes and delivers its report."""
    if action == "list":
        return _jobs_listing(ctx)
    if not id:
        return f"job: action {action!r} needs an id (use action=\"list\" to find it)."
    if action == "output":
        return _job_output_read(ctx, id)
    if action == "wait":
        return await _job_wait(ctx, id, timeout)
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
        # The nesting ceiling isn't bound here: it rides on Deps
        # (subagent_max_depth), where the model can't touch it.
        agent.tool(spawn_agent)
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
        never among them — nested spawning is granted separately by
        SubagentRunner.build, and only above the leaf depth."""
        for name in sorted(set(tool_names)):
            if not self._register_lsp_tools and name in LSP_TOOLS:
                continue
            fn = _SUBAGENT_FNS.get(name)
            if fn is None:
                continue
            agent.tool(fn)
