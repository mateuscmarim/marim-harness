import asyncio
import logging
import re

from pydantic_ai import RunContext

from ..runtime.deps import Deps
from .fs_tools import _scratch_roots
from .impl import fs, shell
from .lenient import LenientList

logger = logging.getLogger(__name__)

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
    """Create or overwrite a file. `path` is relative to the workspace root, or
    an absolute path inside the session scratchpad directory."""
    # This is ``async def`` so it can ``await _with_diagnostics`` — but that signature
    # opts out of pydantic-ai's auto thread-offload (it awaits async tools directly on
    # the event loop). ``fs.write_file`` does a blocking read + atomic write + double
    # fsync, so run it in a worker thread to keep the loop free for other tool calls.
    result = await asyncio.to_thread(
        fs.write_file, ctx.deps.workspace.root, path, content, ctx.deps.reads,
        _scratch_roots(ctx),
    )
    return await _with_diagnostics(ctx, path, result)


async def edit_file(ctx: RunContext[Deps], path: str, edits: LenientList[fs.Edit]) -> str:
    """Apply one or more find/replace edits to a file, in order and
    all-or-nothing. Each edit is {old_string, new_string, replace_all?};
    old_string must match exactly once unless replace_all is set. `path` is
    relative to the workspace root, or an absolute path inside the session
    scratchpad directory."""
    # Offload the blocking fs work to a thread (see ``write_file`` above): the async
    # signature exists only to ``await _with_diagnostics``, and would otherwise run the
    # read + atomic write + fsyncs directly on the event loop.
    result = await asyncio.to_thread(
        fs.edit_file, ctx.deps.workspace.root, path, edits, ctx.deps.reads,
        _scratch_roots(ctx),
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
