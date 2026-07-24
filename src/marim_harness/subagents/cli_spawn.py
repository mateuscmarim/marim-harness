"""Runs and resumes ``backend: claude-cli`` spawns — the external ``claude -p``
process path. Owns the CLI-side meta/checkpoint templates and the ``--resume``
relaunch; rejoins the runner's shared ``_run_spawn_lifecycle`` (passed in as
``lifecycle``) so run+failure+finalize stays written once.
"""

from __future__ import annotations

import os
import time
from collections.abc import Callable
from pathlib import Path
from typing import TYPE_CHECKING, Any

from ..hooks.dispatch import TurnHooks
from ..runtime.deps import Deps
from ..runtime.permissions import Mode
from ..workspace import effective_tools
from .backend import CONTINUATION_PROMPT, SpawnLifecycle, SpawnRun
from .isolation import SpawnWorktree
from .persistence import SpawnTranscripts, count_tool_calls

if TYPE_CHECKING:
    from ..workspace.agents import AgentDef
    from .cli_backend import CliResult


class CliSpawnOrchestrator:
    """Runs and resumes ``backend: claude-cli`` spawns on behalf of a
    ``SubagentRunner``. ``lifecycle`` is the runner's bound
    ``_run_spawn_lifecycle`` (the shared run+failure+finalize tail);
    ``resolve_agent`` is the runner's bound ``_resolve_agent``."""

    def __init__(self, deps: Deps, hooks: TurnHooks,
                 transcripts: SpawnTranscripts,
                 lifecycle: SpawnLifecycle,
                 resolve_agent: Callable[[str], AgentDef | None]) -> None:
        self.deps = deps
        self.hooks = hooks
        self._transcripts = transcripts
        self._lifecycle = lifecycle
        self._resolve_agent = resolve_agent

    async def execute(
        self, defn: AgentDef, task: str, work_root: Path | None, iso: SpawnWorktree | None,
        mcp_names: list[str] | None, max_output_chars: int | None,
        model: str | None, stream_id: str, *, background: bool,
        resume_session_id: str | None = None, original_task: str | None = None,
        depth: int = 1, transcript_prefix: list[Any] | None = None,
    ) -> str:
        """Run a ``backend: claude-cli`` agent inside the same lifecycle the native
        path uses: hooks bracketing, output cap/spill, worktree close, background
        persist. Harness MCP grants are NOT forwarded to the CLI (it uses its own
        MCP config); a non-empty ``mcp_names`` is noted, not honored.

        Mirrors _execute_spawn's foreground/background contract: foreground
        contains a failure as an error string (so a sibling fan-out spawn isn't
        taken down); background re-raises to the job registry. Usage is folded into
        the session, and a background spawn persists immediately since no run_turn
        will fold its spend."""
        hook_task = original_task or task
        # Wall-clock start for the terminal meta's duration stat. The native path
        # reads prep.t0 (stamped in _execute_spawn); the CLI early-return branches
        # before prep exists, so stamp its own here. A resumed leg times only
        # itself — same rule as native.
        t0 = time.perf_counter()
        meta: dict | None = None
        checkpoint: Callable[[list, str | None], None] | None = None
        if stream_id:
            # Same template the native path builds in _prepare_spawn, plus the two
            # CLI-only keys: `backend` routes resume_spawn to the CLI branch, and
            # `cli_session_id` (filled by the first checkpoint once the init event
            # arrives) is the `claude -p --resume` key. Mutating the shared
            # template between checkpoints is safe — TranscriptStore.write
            # snapshots the dict before stamping.
            meta = {
                "stream_id": stream_id, "type": defn.name, "task": hook_task,
                "model": model, "mcp": None, "depth": depth,
                "max_output_chars": max_output_chars,
                "isolation": iso.branch if iso else None,
                "status": "running",
                "backend": "claude-cli",
                "cli_session_id": resume_session_id,
            }

            def _checkpoint(messages: list, session_id: str | None,
                           _meta=meta) -> None:
                if session_id:
                    _meta["cli_session_id"] = session_id
                # The resumed process's stream carries only the CONTINUATION —
                # `claude -p --resume` does not re-emit the prior history. Without
                # the prefix, this checkpoint would overwrite the sidecar with
                # tail-only content, destroying the interrupted segment (incl. the
                # demuxed-children entries) the pane replays (spec §4). On a fresh
                # spawn transcript_prefix is None, so this is a plain passthrough.
                # cap_transcript (inside TranscriptStore.write) bounds the combined
                # payload.
                self._transcripts.save(stream_id, (transcript_prefix or []) + messages,
                                       meta=_meta, cap_reasoning=True)

            checkpoint = _checkpoint

        await self.hooks.subagent_start(defn.name, hook_task)
        # A resumed CLI spawn (resume_session_id set) keeps its branch on failure —
        # native-resume parity; a fresh spawn's branch is throwaway.
        resumed = resume_session_id is not None
        async def _run() -> SpawnRun:
            result = await self.run_cli(
                defn, task, work_root, model, stream_id,
                checkpoint=checkpoint, resume_session_id=resume_session_id,
            )
            # Same prefix rule as the checkpoint above: the final write is also
            # tail-only for a resumed run, so prepend the pre-interrupt segment.
            full_transcript = (transcript_prefix or []) + result.transcript
            final_meta = None
            if meta is not None:
                final_meta = {
                    **meta,
                    "status": "finished",
                    "cli_session_id": result.session_id or meta["cli_session_id"],
                    "usage": {"input": result.usage.input_tokens,
                              "output": result.usage.output_tokens},
                    "tool_count": count_tool_calls(full_transcript),
                    "duration": time.perf_counter() - t0,
                }
            # child_transcripts carries the demuxed Claude-side Agent/Task
            # sub-agents for _finalize_spawn to persist under their own stream ids.
            return SpawnRun(
                output=result.output,
                transcript=full_transcript,
                usage=result.usage,
                final_meta=final_meta,
                child_transcripts=result.child_transcripts,
            )

        # The CLI path now rides the SAME lifecycle as native — the deliberate
        # duplication (and its `if background` fork) is gone. timing=None (a CLI
        # spawn keeps no time-to-first-token); note is the not-forwarded-MCP note.
        # A cancelled CLI spawn now close()s its worktree like native (committing
        # in-progress work, keeping the branch) instead of discard()ing it — fixing
        # the resume-after-cancel divergence.
        return await self._lifecycle(
            _run, iso=iso, resumed=resumed, background=background, name=defn.name,
            stop_task=hook_task, note=self._mcp_note(mcp_names),
            max_output_chars=max_output_chars, stream_id=stream_id, timing=None,
        )

    @staticmethod
    def _mcp_note(mcp_names: list[str] | None) -> str:
        """A one-line note when the orchestrator named MCP servers for a CLI spawn:
        they aren't forwarded (the CLI uses its own MCP config), so say so rather
        than silently dropping them."""
        if not mcp_names:
            return ""
        names = ", ".join(mcp_names)
        return (
            f"[note: MCP servers ({names}) are not forwarded to claude-cli "
            "sub-agents; configure them in the CLI's own settings]\n\n"
        )

    async def run_cli(self, defn: AgentDef, task: str, work_root: Path | None,
                      model: str | None, stream_id: str,
                      checkpoint: Callable[[list, str | None], None] | None = None,
                      resume_session_id: str | None = None) -> CliResult:
        """Resolve binary, tool reach, model, and cwd for a CLI spawn, then run it.
        Raises CliUnavailable when no `claude` binary is found so the caller's
        contained-error path reports it. Reach mirrors the native gate — gated
        tools only in auto mode. Model precedence: per-spawn override, then the
        agent's frontmatter model, then $MARIM_CLAUDE_CLI_MODEL, then the CLI's
        own default."""
        from ..tools.names import NET_TOOLS
        from .cli_backend import (
            CLI_MODEL_ENV,
            ClaudeCliRunner,
            CliUnavailable,
            map_tools_to_cc,
            resolve_cli_binary,
        )

        binary = resolve_cli_binary()
        if binary is None:
            raise CliUnavailable(
                "no `claude` binary found (set MARIM_CLAUDE_CLI_BIN or install "
                "Claude Code)"
            )
        # Same spawn-time mode snapshot as the native path (SubagentRunner.build):
        # reach is fixed when the CLI process launches; a mode flip affects the
        # next spawn. Plan mode strips web_search/fetch_url from the grant AND
        # hard-denies their Claude Code counterparts (WebSearch/WebFetch) via
        # --disallowedTools. The strip alone is NOT enough: --allowedTools is
        # additive pre-approval only, and the CLI degrades every non-auto mode
        # to `--permission-mode plan`, whose own policy auto-allows the web
        # research tools — so absence from the allowlist (or an allowlist
        # omitted entirely when the stripped set maps empty) denies nothing.
        # --disallowedTools is the deny headless `claude -p` actually honors,
        # closing marim's plan-mode egress boundary (_plan_decision) on this
        # backend too. Only the two net tools are denied — the rest of the
        # CLI's reach stays governed by its own permission mode.
        mode = self.deps.workspace.mode
        allow_gated = mode is Mode.auto
        deny_net = mode is Mode.plan
        tools = effective_tools(defn, allow_gated=allow_gated, allow_net=not deny_net)
        cwd = str(work_root or self.deps.workspace.root)
        model_name = model or defn.model or os.environ.get(CLI_MODEL_ENV)
        cbs = self.deps.ui
        runner = ClaudeCliRunner(
            cbs.on_subagent_event, cbs.on_subagent_notice, cbs.on_subagent_model
        )
        result = await runner.run(
            binary=binary, prompt=task, system_prompt=defn.prompt, cwd=cwd,
            allow_gated=allow_gated, allowed_tools=tools, model=model_name,
            disallowed_tools=map_tools_to_cc(NET_TOOLS) if deny_net else None,
            stream_id=stream_id, checkpoint=checkpoint,
            resume_session_id=resume_session_id,
        )
        if stream_id and cbs.on_subagent_usage is not None:
            await cbs.on_subagent_usage(stream_id, result.usage)
        return result

    async def resume(self, stream_id: str, meta: dict) -> tuple[str | None, str]:
        """Resume an interrupted claude-cli spawn by relaunching the CLI with
        ``--resume`` on its recorded session id, as a background job. The caller
        (resume_spawn) already holds the ``_resuming`` guard and has verified the
        sidecar status and the absence of a live job. There is deliberately no
        pre-flight check that the CLI session file still exists — its on-disk
        scheme is CLI-internal, so a stale session surfaces as the CLI's own
        error on the failed job instead of a brittle path probe here."""
        session_id = meta.get("cli_session_id")
        if not session_id:
            return None, ("The CLI session id was never recorded (the spawn died "
                          "before its session started) — nothing to resume; "
                          "spawn it again instead.")
        type_ = str(meta.get("type") or "")
        task = str(meta.get("task") or "")
        defn = self._resolve_agent(type_)
        if defn is None:
            return None, f"No sub-agent type {type_!r} anymore — can't resume."
        if defn.backend != "claude-cli":
            return None, (f"Sub-agent type {type_!r} is no longer claude-cli "
                          "backed — can't resume its CLI session.")
        iso = None
        branch = meta.get("isolation")
        if branch:
            iso, err = SpawnWorktree.reopen(self.deps.workspace.root, branch)
            if err is not None:
                return None, err
        # Read the previously persisted transcript before relaunching. The resumed
        # CLI process's stream carries only the continuation (`claude -p --resume`
        # does not re-emit prior history), so the resume's checkpoints and final
        # write must PREPEND this prefix or they'd overwrite the sidecar with
        # tail-only content, destroying the pre-interrupt segment (incl. the
        # demuxed children) the pane replays (spec §4). Best-effort: an unreadable
        # transcript yields [], so the resume proceeds tail-only rather than
        # refusing — resumability trumps a perfect replay.
        prior = self._transcripts.read(stream_id) or []
        label = f"{type_}: resumed — {task}"
        job_id = self.deps.jobs.register(
            "agent", label,
            self.execute(
                defn, CONTINUATION_PROMPT,
                iso.path if iso else None, iso,
                None, meta.get("max_output_chars"), meta.get("model"), stream_id,
                background=True, resume_session_id=session_id,
                original_task=task, depth=int(meta.get("depth") or 1),
                transcript_prefix=prior,
            ),
            stream_id=stream_id,
            prompt=task,
        )
        return job_id, f"Resumed as {job_id}."
