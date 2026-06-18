import json
import re
from typing import Iterable, Literal, Optional, Protocol

from pydantic_ai import RunContext

from ..deps import Deps, HarnessAgent, SubAgent
from ..jobs import render_jobs
from ..tasks import Task, summarize
from ..workspace.agents import compose_subagent_task
from ..workspace.memory import global_scope, project_scope, read_memory, save_memory
from ..workspace.skills import find_skill, read_bundled_file, read_skill_body
from . import fetch, fs, shell, web

# Re-exported for backward compatibility; defined in the leaf module ``names``
# so importers (e.g. workspace.agents) don't pull in all of ``provider`` and
# form an import cycle.
from .names import GATED_TOOLS, LSP_TOOLS, NET_TOOLS, READ_TOOLS, SUBAGENT_TOOLS  # noqa: F401

_BASH_TIMEOUT = 60


# --- tool implementations (module-level so they can be registered onto the main
# agent gated, or onto a sub-agent plain, from a single source of truth) ---


async def fetch_url(
    ctx: RunContext[Deps],
    url: str,
    prompt: Optional[str] = None,
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
        url, prompt=prompt, workspace_root=ctx.deps.workspace_root
    )


def read_file(
    ctx: RunContext[Deps], path: str, offset: int = 1, limit: Optional[int] = None
) -> str:
    """Read a text file. `path` is relative to the workspace root.

    For large files, read a window instead of the whole thing: `offset` is the
    1-based line to start at and `limit` caps the line count. Prefer locating
    what you need first (with `grep`/`tree`) and reading a targeted range — a
    read with no `limit` is capped and will tell you how to page on."""
    return fs.read_file(ctx.deps.workspace_root, path, offset=offset, limit=limit)


def glob(ctx: RunContext[Deps], pattern: str) -> str:
    """List files matching a glob pattern (e.g. `**/*.py`)."""
    return fs.glob_files(ctx.deps.workspace_root, pattern)


def tree(ctx: RunContext[Deps], path: str = ".", depth: int = 2) -> str:
    """Show a directory tree. `depth=1` lists one level (like ls); higher
    descends further. Noise dirs (.git, node_modules, …) aren't expanded."""
    return fs.tree(ctx.deps.workspace_root, path, depth)


def grep(ctx: RunContext[Deps], pattern: str, path: Optional[str] = None) -> str:
    """Search file contents for a regex. Optionally scope to `path`."""
    return fs.grep(ctx.deps.workspace_root, pattern, path)


_LSP_UNAVAILABLE = "LSP is not available in this session."


async def goto_definition(ctx: RunContext[Deps], path: str, line: int, col: int) -> str:
    """Jump to where the symbol at `path:line:col` is defined, returning the
    target location(s) as `path:line:col`. Coordinates are 1-based — read them
    off `read_file`/`grep` output. Prefer this over grepping for a definition."""
    if ctx.deps.lsp is None:
        return _LSP_UNAVAILABLE
    return await ctx.deps.lsp.goto_definition(path, line, col)


async def find_references(ctx: RunContext[Deps], path: str, line: int, col: int) -> str:
    """List every use of the symbol at `path:line:col` across the project, as
    `path:line:col` lines. Coordinates are 1-based. Use before renaming or
    removing a symbol to see its blast radius."""
    if ctx.deps.lsp is None:
        return _LSP_UNAVAILABLE
    return await ctx.deps.lsp.find_references(path, line, col)


async def hover(ctx: RunContext[Deps], path: str, line: int, col: int) -> str:
    """Show the type/signature and docs for the symbol at `path:line:col`
    (1-based), as the language server's hover text. Use to learn a value's type
    without opening its definition."""
    if ctx.deps.lsp is None:
        return _LSP_UNAVAILABLE
    return await ctx.deps.lsp.hover(path, line, col)


async def document_symbols(ctx: RunContext[Deps], path: str) -> str:
    """Outline one file: its classes, functions, and methods with line numbers.
    A fast way to understand a file's shape before reading it in full."""
    if ctx.deps.lsp is None:
        return _LSP_UNAVAILABLE
    return await ctx.deps.lsp.document_symbols(path)


async def workspace_symbols(ctx: RunContext[Deps], query: str) -> str:
    """Find a symbol by name across the whole project, returning matches as
    `name  path:line`. Use to locate a class/function when you know its name but
    not its file."""
    if ctx.deps.lsp is None:
        return _LSP_UNAVAILABLE
    return await ctx.deps.lsp.workspace_symbols(query)


async def diagnostics(ctx: RunContext[Deps], path: str) -> str:
    """Report the language server's errors and warnings for `path`, as
    `path:line:col: severity: message`. Edits already append fresh diagnostics
    automatically; call this to re-check a file on demand."""
    if ctx.deps.lsp is None:
        return _LSP_UNAVAILABLE
    return await ctx.deps.lsp.diagnostics(path)


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


def update_tasks(ctx: RunContext[Deps], tasks: list[Task]) -> str:
    """Maintain your checklist for the current multi-step task. Pass the
    FULL list every time — it replaces the previous one. Each item is
    {text, status} where status is pending, in_progress, or done. Keep
    exactly one item in_progress, and mark items done as you finish them.
    Use this for non-trivial work spanning several steps so progress is
    visible; skip it for single-step requests. No approval is needed."""
    ctx.deps.tasks.replace(tasks)
    return summarize(ctx.deps.tasks.items)


async def web_search(
    ctx: RunContext[Deps],
    query: str,
    categories: Optional[str] = None,
    max_results: int = 10,
) -> str:
    """Search the web via a self-hosted SearXNG instance and return formatted results.

    *query* is the search string.  *categories* restricts results to a SearXNG
    category (e.g. "general", "images", "news", "science").  *max_results*
    caps how many hits are returned (default 10, max 50)."""
    return await web.web_search(query, categories=categories, max_results=max_results)


def _coerce_mcp(mcp: "list[str] | str | None") -> Optional[list[str]]:
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


async def spawn_agent(
    ctx: RunContext[Deps],
    type: str,
    task: str,
    background: bool = False,
    mcp: list[str] | str | None = None,
    max_output_chars: int | None = None,
    returns: str | None = None,
    constraints: str | None = None,
    context: str | None = None,
) -> str:
    """Delegate a sub-task to an isolated sub-agent that runs on the same model
    and reports back. `type` is a built-in — `explore` (read-only investigation;
    reports findings, changes nothing) or `general` (full toolset; carries out a
    focused sub-task autonomously) — or a custom agent by name, as listed in the
    sub-agents index. The sub-agent starts with a clean context, does `task`, and
    its final message becomes this tool's result. Spawn several in one turn to
    fan out independent work; sub-agents cannot spawn further sub-agents.

    Set `background=True` to launch it as a detached job and return immediately
    with a job id instead of waiting — keep working, then read its report later
    with job_output / wait_for_job. Background sub-agents don't stream their
    steps; you only see the final report when you pull it.

    `mcp` grants the sub-agent specific MCP servers by name (none by default).
    Pass the names listed as enabled in the sub-agents index — e.g.
    `mcp=["mddocs"]` lets the sub-agent use that server's tools, gated the same
    way your own MCP calls are. Unknown or disabled names are ignored and noted
    in the report.

    `max_output_chars` caps the report this spawn returns into your context — set
    it when you're fanning out and want bounded inflow. It's a budget the
    sub-agent distills toward (it's told to lead with the conclusion and
    summarize to fit), not a blind truncation. For a foreground spawn it's also
    enforced losslessly: a report over budget is written to a workspace file and
    replaced with a within-budget head + a pointer to that file, so nothing is
    lost — you can read the file if you need the detail. Leave it unset for an
    unbounded report. (For a background spawn it applies as a soft instruction
    only.)

    `returns`, `constraints`, and `context` are optional structured fields folded
    into the sub-agent's prompt — all freeform text, all additive (omit any and
    nothing changes). Use them to give a clean-context sub-agent what it can't
    infer: `returns` is the output contract (what to hand back and in what shape —
    the highest-leverage field, since otherwise you get a shape you have to re-ask
    for); `constraints` are boundaries on how to work (a soft nudge — real tool
    reach is still set by `type`/`mcp`, not prose); `context` is the orchestration-
    level background it can't see (why this task, what's already known). The plain
    `task` stays the one required ask."""
    mcp_names = _coerce_mcp(mcp)
    task = compose_subagent_task(
        task, returns=returns, constraints=constraints, context=context
    )
    if background:
        if ctx.deps.run_background_agent is None:
            return "Background sub-agents are not available in this context."
        label = f"{type}: {task}"
        job_id = ctx.deps.jobs.register(
            "agent", label,
            ctx.deps.run_background_agent(type, task, mcp_names, max_output_chars),
        )
        return f"Started {job_id} (agent) — {label[:60]}"
    if ctx.deps.run_subagent is None:
        return "Sub-agents are not available in this context."
    return await ctx.deps.run_subagent(
        type, task, ctx.tool_call_id or "", mcp_names, max_output_chars
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
    model should fix. Never raises: any failure leaves ``result`` untouched."""
    if ctx.deps.lsp is None:
        return result
    try:
        report = await ctx.deps.lsp.diagnostics(path, settle=0.8)
    except Exception:  # noqa: BLE001 — diagnostics must never fail an edit
        return result
    if not report or not _DIAGNOSTIC_LINE.search(report):
        return result
    return f"{result}\n\ndiagnostics:\n{report}"


async def write_file(ctx: RunContext[Deps], path: str, content: str) -> str:
    """Create or overwrite a file. `path` is relative to the workspace root."""
    result = fs.write_file(ctx.deps.workspace_root, path, content)
    return await _with_diagnostics(ctx, path, result)


async def edit_file(ctx: RunContext[Deps], path: str, edits: list[fs.Edit]) -> str:
    """Apply one or more find/replace edits to a file, in order and
    all-or-nothing. Each edit is {old_string, new_string, replace_all?};
    old_string must match exactly once unless replace_all is set."""
    result = fs.edit_file(ctx.deps.workspace_root, path, edits)
    return await _with_diagnostics(ctx, path, result)


async def bash(ctx: RunContext[Deps], command: str, background: bool = False) -> str:
    """Run a shell command in the workspace root.

    Set `background=True` for long-running commands (dev servers, builds, test
    watchers): the command is launched detached and the tool returns immediately
    with a job id instead of blocking. Check on it later with job_output /
    wait_for_job, or stop it with cancel_job. A foreground run (the default) waits
    for the command and is subject to a timeout, so use background for anything
    that won't finish promptly."""
    reason = ctx.deps.command_policy.check(command)
    if reason is not None:
        return f"Blocked by command policy: {reason}"
    if background:
        bp = await shell.start_bash(ctx.deps.workspace_root, command)
        job_id = ctx.deps.jobs.register(
            "bash", command, bp.wait(), kill=bp.kill, output_fn=bp.output
        )
        return f"Started {job_id} (bash) — {command[:60]}"
    return await shell.run_bash(ctx.deps.workspace_root, command)


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
    return ctx.deps.jobs.output(id)


async def wait_for_job(ctx: RunContext[Deps], id: str, timeout: float = 60) -> str:
    """Block until a background job finishes (up to `timeout` seconds), then
    return its result. If it's still running when the timeout elapses, the job
    keeps going and you get a "still running" note — call again or check
    job_output later. Use this when you need a job's result before continuing."""
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
    `id` is required for every action except "list"; `timeout` applies only to "wait"."""
    if action == "list":
        return render_jobs(ctx.deps.jobs.list()) or "No background jobs."
    if not id:
        return f"job: action {action!r} needs an id (use action=\"list\" to find it)."
    if action == "output":
        return ctx.deps.jobs.output(id)
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
        separately through ``deps.lsp`` and is unaffected by this flag.

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
        agent.tool(requires_approval=True, timeout=_BASH_TIMEOUT)(bash)

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
            if name == "bash":
                agent.tool(timeout=_BASH_TIMEOUT)(fn)
            else:
                agent.tool(fn)
