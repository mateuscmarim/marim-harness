"""Optional Claude Code backend for sub-agents: one long-lived, bidirectional
``claude`` per spawn.

A ``backend: claude-cli`` sub-agent runs on the user's Claude subscription: the
spawn's task goes down a ``ClaudeProcess`` (``claude/process.py``) as one
stream-json turn, Claude's tool calls are approved per tool through the spawn's
``ClaudeApprovalBroker`` (marim's ``auto``/``ask``/``plan`` mode, the approval
panel, ``ask_user``), and the assistant/user objects it emits are translated
into pydantic-ai streaming events for the sub-agents screen. The process is
closed when the spawn ends — after the turn Claude runs on its own once a
background Agent of its own reports, since that agent lives inside the
process (the LAST result's text is the spawn's report, see
``sum_result_usages``); an interrupted spawn resumes by session id.

The harness wrapping (worktree, hooks bracketing, output cap, background
persist) stays in ``cli_spawn.py``, so this module is unit-tested without the
rest of the harness.
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable, Iterable
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import TYPE_CHECKING

from pydantic_ai.messages import (
    FunctionToolCallEvent,
    FunctionToolResultEvent,
    ModelRequest,
    ModelResponse,
    PartDeltaEvent,
    PartStartEvent,
    TextPart,
    TextPartDelta,
    ThinkingPart,
    ThinkingPartDelta,
    ToolCallPart,
    ToolReturnPart,
)
from pydantic_ai.usage import RunUsage

from ..claude.env import (
    CLI_BINARY_ENV,  # noqa: F401 — re-exported for tests
    CLI_MODEL_ENV,  # noqa: F401 — re-exported for tests
    CLI_TIMEOUT_ENV,  # noqa: F401 — re-exported for tests
    CliUnavailable,  # noqa: F401 — re-exported: cli_spawn imports this here
    resolve_cli_binary,  # noqa: F401 — re-exported for tests/config/settings
)
from ..claude.env import DEFAULT_CLI_TIMEOUT as _DEFAULT_CLI_TIMEOUT  # noqa: F401 — re-exported
from ..claude.env import cli_timeout as _cli_timeout
from ..claude.lifecycle import ClaudeLifecycle
from ..claude.process import ClaudeProcess, ProcessOptions, turn_objects
from ..claude.protocol import CLOSED
from ..config.external_cli import CliModelError
from ..config.lifecycle import BackendNotice, deliver_child_notice, notice_part

if TYPE_CHECKING:
    from ..claude.approvals import ClaudeApprovalBroker
    from .cli_demux import CliSubagentDemux, RoutedEvent

logger = logging.getLogger(__name__)


# Harness tool name → Claude Code tool name. Names with no Claude Code equivalent
# (tree, the LSP navigation tools) are absent on purpose: the CLI has its own
# navigation, so we don't fabricate a mapping. The result feeds --tools.
_CC_TOOL_MAP = {
    "read_file": "Read",
    "glob": "Glob",
    "grep": "Grep",
    "web_search": "WebSearch",
    "fetch_url": "WebFetch",
    "write_file": "Write",
    "edit_file": "Edit",
    "bash": "Bash",
}

# Claude Code tool name → harness tool name (the inverse of _CC_TOOL_MAP). The TUI
# keys all its rich rendering (the edit diff, write/read highlighting, the tool
# summary labels) on harness names and arg shapes, so a CLI sub-agent's events are
# normalized back to those before they reach the renderer — otherwise an `Edit`
# falls through to a raw-args dump instead of the inline diff a native edit shows.
_HARNESS_TOOL_MAP = {cc: harness for harness, cc in _CC_TOOL_MAP.items()}


def normalize_cc_tool(name: str, args: dict) -> tuple[str, dict]:
    """Map a Claude Code tool_use (name + ``input``) to the harness tool name and
    arg shape the TUI widgets expect, so a CLI spawn renders identically to a
    native call. Unmapped tools (TodoWrite, Task, …) pass through unchanged for
    generic rendering.

    The harness ``fs.Edit`` model already shares Claude Code's field names
    (``old_string``/``new_string``/``replace_all``), so an Edit maps to a single
    such edit wrapped in the ``edits`` list ``edit_file`` renders from. Read/Write
    just rename ``file_path`` → ``path``; the rest share their arg keys
    (``pattern``/``command``/``query``)."""
    harness = _HARNESS_TOOL_MAP.get(name)
    if harness is None:
        return name, args
    if name == "Edit":
        return harness, {
            "path": args.get("file_path", ""),
            "edits": [
                {
                    "old_string": args.get("old_string", ""),
                    "new_string": args.get("new_string", ""),
                    "replace_all": bool(args.get("replace_all", False)),
                }
            ],
        }
    if name in ("Read", "Write") and "file_path" in args:
        out = {k: v for k, v in args.items() if k != "file_path"}
        out["path"] = args["file_path"]
        return harness, out
    return harness, args


class CliRunError(Exception):
    """The CLI ran but produced no terminal result event (crash / bad output)."""


@dataclass
class CliResult:
    """A finished CLI spawn, shaped like the bits of a Pydantic AI run result the
    spawn lifecycle consumes: the final report text, the run's usage, and the
    transcripts — the parent's, plus one per Claude-side child sub-agent (keyed
    by the child's stream id) for sidecar persistence."""

    output: str
    usage: RunUsage
    transcript: list = field(default_factory=list)
    child_transcripts: dict = field(default_factory=dict)
    # The Claude session id captured from the stream's init event — the resume
    # key for `claude -p --resume`. None when the stream never reported one.
    session_id: str | None = None


def map_tools_to_cc(tool_names) -> list[str]:
    """Translate granted harness tool names to Claude Code ``--tools`` names,
    dropping any without a Claude Code equivalent. Sorted for a stable argv
    (and stable tests)."""
    return sorted({_CC_TOOL_MAP[n] for n in tool_names if n in _CC_TOOL_MAP})


def synth_usage(
    cli_usage: dict | None,
    num_turns: int,
    total_cost_usd: float | None = None,
) -> RunUsage:
    """Build a RunUsage from the CLI ``result`` event's ``usage`` block.
    When ``total_cost_usd`` is provided (the CLI's billed amount), it is stored
    in ``details[COST_DETAIL_KEY]`` as integer micro-USD so ``resolve_cost``
    surfaces it as the exact cost — no model-id lookup needed. Missing token
    keys default to 0.

    The CLI reports Anthropic's *raw* usage, where ``input_tokens`` is the
    uncached prompt tokens only — the cache read/write buckets are reported
    separately and are NOT included in it. But the rest of the harness (and
    pydantic-ai/genai-prices for the native path) treats ``RunUsage.input_tokens``
    as *inclusive* of cache: ``split_tokens`` recovers the uncached bucket as
    ``input_tokens - cache_read - cache_write``, and ``total_tokens`` is
    ``input_tokens + output_tokens``. So we fold the cache buckets into
    ``input_tokens`` here, exactly as genai-prices does for a native Anthropic
    response. Without this the uncached split underflowed to 0 (the reported ``↑``
    was always zero) and the token total omitted all cached tokens."""
    from ..usage import COST_DETAIL_KEY

    u = cli_usage or {}
    details: dict = {}
    if total_cost_usd is not None:
        # round(), not int(): truncation loses up to a full microdollar and
        # amplifies float artifacts (a billed 1.001 → 1000999.9999999999 →
        # int() 1000999, one micro-USD short). This matches the other accounting
        # path, config/openrouter_cost.py, which also round()s to micro-USD.
        details[COST_DETAIL_KEY] = round(total_cost_usd * 1_000_000)
    uncached_in = int(u.get("input_tokens", 0) or 0)
    cache_read = int(u.get("cache_read_input_tokens", 0) or 0)
    cache_write = int(u.get("cache_creation_input_tokens", 0) or 0)
    return RunUsage(
        input_tokens=uncached_in + cache_read + cache_write,  # inclusive of cache
        output_tokens=int(u.get("output_tokens", 0) or 0),
        cache_read_tokens=cache_read,
        cache_write_tokens=cache_write,
        requests=int(num_turns or 0),
        details=details,
    )


def sum_result_usages(results: list[dict]) -> tuple[dict, int, float | None]:
    """Fold one CLI run's ``result`` events into ``(usage, num_turns, cost)``
    ready for ``synth_usage`` / ``request_usage_from_cli``.

    One ``claude -p`` process can emit SEVERAL result events: an async
    sub-agent's completion notification re-invokes the main agent, which ends
    in another result. Token buckets are per-segment, so they are summed;
    ``total_cost_usd`` is cumulative across the whole process, so the LAST
    value is the run's cost (both verified against a live 2.1.198 stream).
    Nested non-numeric usage values (``cache_creation``, ``server_tool_use``)
    are skipped."""
    summed: dict = {}
    turns = 0
    cost: float | None = None
    for r in results:
        for k, v in (r.get("usage") or {}).items():
            if isinstance(v, (int, float)):
                summed[k] = summed.get(k, 0) + v
        turns += int(r.get("num_turns", 0) or 0)
        if r.get("total_cost_usd") is not None:
            cost = float(r["total_cost_usd"])
    return summed, turns, cost


class CliStreamTranslator:
    """Turns parsed Claude Code stream-json objects into the Pydantic AI message
    events the TUI already renders, so a CLI spawn streams nested under its card
    like a native sub-agent. Stateful across a run: numbers parts and remembers
    each tool_use's name so the matching tool_result can be labeled. ``translate``
    returns zero or more events per object; ``system`` and the terminal ``result``
    yield nothing (the runner reads result text/usage separately).

    stream-json without ``--include-partial-messages`` delivers each assistant
    message whole, so a text block becomes an empty part-start plus one full
    delta — the render path's delta branch appends it exactly as for live tokens.
    Thinking blocks render as collapsed thoughts via PartStartEvent(ThinkingPart) +
    PartDeltaEvent(ThinkingPartDelta). The ``record_call`` and ``record_return``
    methods exist for the demux to synthesize tool call/return pairs (e.g. for
    Claude's own sub-agents) into the transcript so a persisted sidecar never
    carries an unanswered call.
    """

    def __init__(self) -> None:
        self._index = 0
        self._call_names: dict[str, str] = {}
        self._messages: list = []
        self._lifecycle = ClaudeLifecycle()

    def translate(self, obj: dict) -> list:
        kind = obj.get("type")
        if kind == "assistant":
            return self._assistant(obj)
        if kind == "user":
            return self._user(obj)
        if kind == "system":
            notices = [n for n in self._lifecycle.consume(obj) if isinstance(n, BackendNotice)]
            for notice in notices:
                self._messages.append(ModelResponse(parts=[notice_part(notice.to_payload())]))
            return notices
        return []

    def _assistant(self, obj: dict) -> list:
        events: list = []
        resp_parts = []
        for block in obj.get("message", {}).get("content", []):
            btype = block.get("type")
            if btype == "text":
                idx = self._index
                self._index += 1
                events.append(PartStartEvent(index=idx, part=TextPart(content="")))
                events.append(
                    PartDeltaEvent(
                        index=idx,
                        delta=TextPartDelta(content_delta=block.get("text", "")),
                    )
                )
                resp_parts.append(TextPart(content=block.get("text", "")))
            elif btype == "thinking":
                idx = self._index
                self._index += 1
                events.append(PartStartEvent(index=idx, part=ThinkingPart(content="")))
                events.append(
                    PartDeltaEvent(
                        index=idx,
                        delta=ThinkingPartDelta(content_delta=block.get("thinking", "")),
                    )
                )
                resp_parts.append(ThinkingPart(content=block.get("thinking", "")))
            elif btype == "tool_use":
                call_id = block.get("id", "")
                name, args = normalize_cc_tool(
                    block.get("name", "tool"),
                    block.get("input", {}) or {},
                )
                self._call_names[call_id] = name  # the matching result reuses it
                events.append(
                    FunctionToolCallEvent(
                        part=ToolCallPart(
                            tool_name=name,
                            args=args,
                            tool_call_id=call_id,
                        )
                    )
                )
                resp_parts.append(ToolCallPart(tool_name=name, args=args, tool_call_id=call_id))
        if resp_parts:
            self._messages.append(ModelResponse(parts=resp_parts))
        return events

    def _user(self, obj: dict) -> list:
        events: list = []
        req_parts = []
        for block in obj.get("message", {}).get("content", []):
            if block.get("type") != "tool_result":
                continue
            call_id = block.get("tool_use_id", "")
            part = ToolReturnPart(
                tool_name=self._call_names.get(call_id, "tool"),
                content=_flatten_tool_result(block.get("content")),
                tool_call_id=call_id,
                timestamp=datetime.now(tz=timezone.utc),
                outcome="failed" if block.get("is_error") else "success",
            )
            events.append(FunctionToolResultEvent(part=part))
            req_parts.append(part)
        if req_parts:
            self._messages.append(ModelRequest(parts=req_parts))
        return events

    def transcript(self) -> list:
        """The run so far as pydantic-ai messages (for transcript persistence)."""
        return list(self._messages)

    def record_call(self, part: ToolCallPart) -> None:
        """Append a synthesized tool call (e.g. the demux's spawn_agent for a
        Claude-side sub-agent) to the transcript, and remember its name so a
        later synthesized return — or a raw tool_result hitting translate() —
        labels itself correctly."""
        self._call_names[part.tool_call_id] = part.tool_name
        self._messages.append(ModelResponse(parts=[part]))

    def record_return(self, part: ToolReturnPart) -> None:
        """Append a synthesized tool return, closing a record_call so a
        persisted sidecar never carries an unanswered call."""
        self._messages.append(ModelRequest(parts=[part]))


def _flatten_tool_result(content) -> str:
    """A tool_result's content is either a string or a list of content blocks;
    reduce it to plain text for the card."""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "".join(
            b.get("text", "") for b in content if isinstance(b, dict) and b.get("type") == "text"
        )
    return "" if content is None else str(content)


@dataclass
class _RunState:
    """The turn loop's mutated locals, pulled into one object so they can be
    threaded through `_consume`/`_finalize` without an in/out tuple per call.
    `output` is the last-seen result object's text; `results` accumulates every
    result object for `_finalize`'s usage fold; `model_sent` latches once the
    CLI's reported model has been surfaced to the UI; `session_id` is the resume
    key, deliberately left UNSEEDED by a resume and filled only from the stream,
    so newest-id-wins still holds when `--resume` forks the conversation onto a
    new id; `last_ckpt_len` is the transcript length as of the last checkpoint,
    so growth-only checkpointing can compare against it."""

    output: str = ""
    results: list[dict] = field(default_factory=list)
    model_sent: bool = False
    session_id: str | None = None
    last_ckpt_len: int = 0
    closed_detail: str = ""  # "claude exited (code N): <stderr>" when the process died mid-turn


class ClaudeCliRunner:
    """Runs one sub-agent task on its own ``claude`` process and forwards its
    activity.

    Sends the task as a single turn, translates every object the CLI streams
    back for the UI (when a foreground ``stream_id`` and an ``on_event`` sink
    are present), and captures the terminal ``result`` object's text + usage.
    Raises CliRunError if the turn ends without a result. The harness wraps
    this with hooks, output cap, and worktree handling — see
    CliSpawnOrchestrator.execute.
    """

    def __init__(self, on_event, on_notice, on_model=None) -> None:
        self._on_event = on_event  # Deps.on_subagent_event | None
        # Reserved for the spec's low-fidelity on_subagent_notice fallback; not
        # wired in v1 — full-fidelity event translation via _on_event is always used.
        self._on_notice = on_notice  # Deps.on_subagent_notice | None
        # Surfaces the model the CLI reports (system/init) to the spawn card, which
        # otherwise shows the harness's own model as a fallback. None when no UI.
        self._on_model = on_model  # Deps.on_subagent_model | None

    async def run(
        self,
        *,
        binary: str,
        prompt: str,
        system_prompt: str,
        cwd: str,
        allowed_tools: Iterable[str],
        model: str | None,
        stream_id: str | None,
        disallowed_tools: Iterable[str] | None = None,
        checkpoint: Callable[[list, str | None], None] | None = None,
        resume_session_id: str | None = None,
        broker: ClaudeApprovalBroker | None = None,
    ) -> CliResult:
        """Run one spawn to completion on its own ``claude`` process.

        ``allowed_tools`` are marim tool names (mapped to Claude Code's via
        ``map_tools_to_cc``); ``disallowed_tools`` are Claude Code names the
        caller hard-denies (plan mode's web tools). ``broker`` answers the
        process's ``can_use_tool`` requests; without one the process replies
        with an error response (Claude treats it as a denial) — tests only,
        ``cli_spawn.run_cli`` always builds one. On ``resume_session_id`` the
        system prompt is omitted (the session already has it) and ``prompt``
        is the resume text.

        A silence timeout (``MARIM_CLAUDE_CLI_TIMEOUT``, paused while a prompt
        waits in the panel) interrupts and then kills a hung spawn: it must not
        pin its concurrency slot forever."""
        options = ProcessOptions(
            binary=binary,
            cwd=cwd,
            model=model,
            resume_id=resume_session_id,
            tools=tuple(map_tools_to_cc(allowed_tools)),
            disallowed_tools=tuple(disallowed_tools or ()),
            append_system=None if resume_session_id else system_prompt,
        )
        process = ClaudeProcess(
            options,
            on_request=broker.handle if broker is not None else None,
            silence_timeout=_cli_timeout(),
        )
        from .cli_demux import CliSubagentDemux  # lazy: cli_demux imports us

        translator = CliStreamTranslator()
        demux = CliSubagentDemux()
        # Deliberately unseeded on resume: `claude --resume` may fork into a
        # FRESH session, and the first object it emits carries the new id.
        # Capturing that (rather than pre-seeding `resume_session_id`) is what
        # lets a later re-resume key off the fork, not the exhausted original.
        state = _RunState()

        async def consume(obj: dict) -> None:
            await self._consume(obj, state, translator, demux, stream_id, checkpoint)

        await process.start()
        try:
            handle = await process.send_turn(prompt)
            async for obj in turn_objects(process, handle):
                await consume(obj)
            await self._settle_background(process, state, consume)
        except CliModelError as exc:
            # next_turn_object's silence timeout (already interrupted the turn).
            raise CliRunError(str(exc)) from exc
        finally:
            try:
                # Growth checkpoints run before the next stream object. A CLI
                # interrupted after its last assistant message may never send
                # that next object, so preserve the current partial transcript
                # and resume id even when there will be no successful result.
                if checkpoint is not None:
                    checkpoint(translator.transcript(), state.session_id)
            except Exception:
                logger.warning("Claude spawn final checkpoint failed: %s", stream_id, exc_info=True)
            finally:
                # One process per spawn: even a failed checkpoint must not
                # leave a process running after the spawn relinquishes it.
                await process.aclose()
        return self._finalize(state, translator, demux)

    async def _settle_background(
        self, process: ClaudeProcess, state: _RunState, consume: Callable[[dict], Awaitable[None]]
    ) -> None:
        """The turn ended with a background Agent of Claude's still running:
        it lives inside this process, so closing now would lose its report
        and leave its card spinning. Wait for the CLI's reaction — the turn it
        runs on its own when the agent reports (`ClaudeProcess.wait_background`)
        — and consume it like the sent turn: its result becomes the spawn's
        report (the reaction is what Claude has to say once the agent is
        done), its usage adds up. A notification the CLI never reacted to
        still settles the card through the demux. Bounded by the silence
        timeout, per wait and per streamed object — an agent that outlives
        it is lost with the process, as under the main loop's idle hold, and
        a reaction that goes silent is interrupted (`next_turn_object`) and
        dropped: the sent turn's result stands as the report."""
        if state.closed_detail:
            return  # the process died mid-turn; there is nothing to wait for
        timeout = process.silence_timeout if process.silence_timeout > 0 else None
        try:
            while await process.wait_background(timeout):
                handle = process.take_unsolicited()
                assert handle is not None
                async for obj in turn_objects(process, handle):
                    await consume(obj)
        except CliModelError as exc:
            logger.warning("claude's reaction to its background sub-agent dropped: %s", exc)
        for obj in process.take_prelude():
            await consume(obj)

    async def _consume(
        self,
        obj: dict,
        state: _RunState,
        translator: CliStreamTranslator,
        demux: CliSubagentDemux,
        stream_id: str | None,
        checkpoint: Callable[[list, str | None], None] | None,
    ) -> None:
        """Route one turn object: checkpoint the transcript growth seen so far
        (at the top, so the message that fails to deliver is not lost), capture
        the session id, note a mid-turn death, and split Claude-side sub-agent
        traffic off through the demux. ``stream_event`` deltas are dropped —
        the spawn card renders whole messages."""
        if checkpoint is not None:
            snapshot = translator.transcript()
            if len(snapshot) != state.last_ckpt_len:
                state.last_ckpt_len = len(snapshot)
                checkpoint(snapshot, state.session_id)
        if obj.get("type") == CLOSED:
            code = obj.get("returncode")
            stderr = str(obj.get("stderr") or "").strip()
            state.closed_detail = f"claude exited (code {code}): {stderr}"
            return
        if obj.get("type") == "stream_event":
            return
        if state.session_id is None:
            sid = obj.get("session_id")
            if isinstance(sid, str) and sid:
                state.session_id = sid
        # Claude-side sub-agent traffic (Agent/Task spawns, their child
        # streams, task lifecycle events) is demuxed into per-card streams;
        # whatever remains is this spawn's own main stream.
        routed, remainder = demux.route(obj)
        for r in routed:
            await self._deliver(r, translator, stream_id)
        if remainder is not None:
            await self._dispatch_remainder(remainder, state, translator, stream_id)

    async def _dispatch_remainder(
        self,
        obj: dict,
        state: _RunState,
        translator: CliStreamTranslator,
        stream_id: str | None,
    ) -> None:
        """Handle the portion of one object left after demux routing:
        first-seen model detection, then result-vs-translate dispatch. Split
        out of `_consume` so each half stays under the complexity ceiling;
        mutates `state` in place."""
        if not state.model_sent:
            # The system/init event carries the session model at top level;
            # assistant messages carry it under message.model. Surface the
            # first one seen so the card shows the CLI's real model. Guard
            # message against a non-dict (malformed/future stream shape).
            msg = obj.get("message")
            found = obj.get("model") or (msg.get("model") if isinstance(msg, dict) else None)
            if found:
                state.model_sent = True
                if self._on_model is not None and stream_id:
                    await self._on_model(stream_id, str(found))
        if obj.get("type") == "result":
            # A turn ends at its result; a spawn sees more than one when
            # Claude reacts to a background Agent's report after it
            # (`_settle_background`). The LAST result's text is the report,
            # usage sums across all of them — see sum_result_usages.
            state.results.append(obj)
            state.output = obj.get("result", "") or ""
            return
        for event in translator.translate(obj):
            if isinstance(event, BackendNotice):
                await deliver_child_notice(event, stream_id, self._on_event, self._on_notice)
            elif self._on_event is not None and stream_id:
                await self._on_event(stream_id, event, None)

    def _finalize(
        self, state: _RunState, translator: CliStreamTranslator, demux: CliSubagentDemux
    ) -> CliResult:
        """Raise CliRunError when the turn never produced a result object (the
        process died — `closed_detail` carries its exit code and stderr — or
        the stream simply ended), else build the finished CliResult."""
        if not state.results:
            detail = state.closed_detail or "the turn ended without a result object"
            raise CliRunError(f"claude produced no result ({detail})")
        return CliResult(
            output=state.output,
            usage=synth_usage(*sum_result_usages(state.results)),
            transcript=translator.transcript(),
            child_transcripts=demux.child_transcripts(),
            session_id=state.session_id,
        )

    async def _deliver(
        self, routed: RoutedEvent, translator: CliStreamTranslator, stream_id: str | None
    ) -> None:
        """Forward one demux-routed event. Main-routed events (the synthesized
        spawn_agent call/return for a Claude-side spawn) go to this spawn's own
        stream and are recorded into the parent transcript, so the persisted
        sidecar replays the nested card. Child-routed events go to the child's
        stream with its live usage and (once) its reported model."""
        if routed.stream_id is None:
            part = getattr(routed.event, "part", None)
            if isinstance(part, ToolCallPart):
                translator.record_call(part)
            elif isinstance(part, ToolReturnPart):
                translator.record_return(part)
            if self._on_event is not None and stream_id:
                await self._on_event(stream_id, routed.event, None)
            return
        if routed.model and self._on_model is not None:
            await self._on_model(routed.stream_id, routed.model)
        if isinstance(routed.event, BackendNotice):
            await deliver_child_notice(
                routed.event, routed.stream_id, self._on_event, self._on_notice, routed.usage
            )
        elif self._on_event is not None:
            await self._on_event(routed.stream_id, routed.event, routed.usage)
