"""Run Claude Code as a main-loop model provider over one long-lived,
bidirectional ``claude`` process.

A Claude subscription is reachable only through the ``claude`` CLI, which runs
its own agentic loop — there is no raw per-step model endpoint behind it. So
this provider makes marim a *launcher*: ``ClaudeCliModel`` keeps one ``claude``
process per conversation (``claude/process.py``), sends each user turn down its
stdin as ``stream-json``, and returns a single **text-only** ``ModelResponse``.
Emitting ``ToolCallPart``s here would make pydantic_ai's agent graph try to
execute Claude's tool calls a second time, so Claude's tool activity is folded
into the streamed text (headless) or pushed out-of-band as native tool cards
(``on_activity``). Claude's own Agent/Task sub-agents are split off via
``cli_demux.CliSubagentDemux`` onto ``on_subagent``.

What the bidirectional transport buys over the old one-shot ``claude -p``:
Claude asks marim before every tool through ``can_use_tool`` control requests,
so marim's ``auto``/``ask``/``plan`` modes, the approval panel and ``ask_user``
apply (``claude/approvals.py``); a steer folds into the live turn; an
interrupt is a control request rather than a kill; and the conversation
resumes by session id after an idle close, a crash, or a marim restart.

Prose and thinking arrive as ``stream_event`` deltas; ``assistant`` objects
contribute their ``tool_use`` blocks (their text repeats the deltas) and the
request's ``usage`` — the real prompt size, kept as the model's
``context_report``; ``user`` objects contribute ``tool_result`` blocks. See
``consume_cli_stream``.

Mode, model and thinking reach the process as control requests rather
than launch flags: before each turn ``_sync_controls`` sends whatever differs
from what the process last acknowledged (``claude/controls.py``), so a
``/mode``, ``/model`` or ``/think`` switch takes effect on the next turn
without a respawn. A session switch, ``/new`` or ``/clear`` drops the live
process (``release_conversation``) so the next turn follows the rebound
store's ref instead of continuing the conversation the user left. A
same-provider ``/model`` switch hands the live process over to the new
model object (``adopt``) instead of closing it.

Cost: the ``result``'s ``usage`` is per turn but its ``total_cost_usd`` is
CUMULATIVE over the process (verified live on 2.1.270: 0.0109 → 0.0137 →
0.0162 across three turns), and a resumed process restarts at zero. Charging
it per turn double-counts the ledger, so ``CostMeter`` bills the delta since
the previous result and resets whenever a process is spawned.

Background sub-agents: Claude's Agent tool returns at once and the sub-agent
runs on; when it reports back while no turn is open, the CLI runs a turn of
its own on the notification (``claude/process.py``'s module docstring). The
process buffers that turn and ``on_backend_turn`` tells the harness, which
queues an *autonomous* marim turn (empty typed text); ``_open_turn`` serves
that turn from the buffered handle instead of sending anything, so Claude's
reaction lands in marim's history exactly where it sits in Claude's own. The
sub-agent demux is one per process (not per turn) for the same reason: the
notification that settles a spawn card can arrive turns after the spawn.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncIterator, Iterator
from contextlib import aclosing, asynccontextmanager
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING, cast

from pydantic_ai.messages import FunctionToolCallEvent, ModelResponse, TextPart
from pydantic_ai.models import Model, ModelRequestParameters, StreamedResponse
from pydantic_ai.usage import RequestUsage

from ..claude.approvals import ClaudeApprovalBroker
from ..claude.controls import thinking_controls
from ..claude.env import (
    INSTALL_HINT,
    MIN_CLAUDE_VERSION,
    cli_idle_timeout,
    cli_timeout,
    resolve_cli_binary,
)
from ..claude.process import (
    ClaudeProcess,
    ProcessOptions,
    TurnHandle,
    next_turn_object,
    turn_objects,
)
from ..claude.protocol import CLOSED, ControlError, ProcessClosed
from ..claude.quota import quota_from_usage
from ..runtime.context import strip_turn_context
from ..runtime.permissions import Mode, UiSeams
from ..usage import COST_DETAIL_KEY
from .cli_input import (
    attachment_content,
    claude_input,
    prompt_content,
)
from .cli_input import (
    extract_system as extract_system,
)
from .cli_input import flatten_history as flatten_history
from .cli_input import latest_user_text as latest_user_text
from .context_report import CONTEXT_REPORT_KEY, ContextReport, prompt_tokens
from .external_cli import (
    CLI_ACTIVITY_KEY,
    ActivityLedger,
    CliModelError,
    ExternalCliModel,
    TextFolder,
)
from .quota import QuotaHint

if TYPE_CHECKING:
    from collections.abc import AsyncGenerator, Awaitable, Callable

    from pydantic_ai.messages import ModelMessage
    from pydantic_ai.settings import ModelSettings

    from ..subagents.cli_demux import CliSubagentDemux

logger = logging.getLogger(__name__)

# The provider-side conversation reference persisted on the marim session
# (SessionStore.cli_thread_id) is namespaced so a switch to another external
# CLI never resumes a foreign id.
SESSION_REF_PREFIX = "claude-cli:"


def request_usage_from_cli(cli_usage: dict | None, total_cost_usd: float | None) -> RequestUsage:
    """Build a ``RequestUsage`` from the CLI ``result`` event's usage block.

    Mirrors ``cli_backend.synth_usage`` (which returns a RunUsage for sub-agents):
    Anthropic reports ``input_tokens`` as the uncached bucket only, so we fold the
    cache read/write buckets back in to match the harness's cache-inclusive
    convention, and store the billed cost as integer micro-USD under
    ``details[COST_DETAIL_KEY]`` so the cost display needs no model-id lookup."""
    u = cli_usage or {}
    details: dict = {}
    if total_cost_usd is not None:
        # round(), not int() truncation, so the micro-USD conversion matches
        # openrouter_cost.read_cost_micro_usd and a sub-cent cost isn't floored away.
        details[COST_DETAIL_KEY] = round(total_cost_usd * 1_000_000)
    cache_read = int(u.get("cache_read_input_tokens", 0) or 0)
    cache_write = int(u.get("cache_creation_input_tokens", 0) or 0)
    uncached_in = int(u.get("input_tokens", 0) or 0)
    return RequestUsage(
        input_tokens=uncached_in + cache_read + cache_write,
        output_tokens=int(u.get("output_tokens", 0) or 0),
        cache_read_tokens=cache_read,
        cache_write_tokens=cache_write,
        details=details,
    )


def charge_cost(usage: RequestUsage, cost_usd: float | None) -> RequestUsage:
    """``usage`` with ``cost_usd`` stored under ``details[COST_DETAIL_KEY]``
    (micro-USD, rounded like ``request_usage_from_cli``); unchanged when the
    cost is unknown."""
    if cost_usd is None:
        return usage
    return RequestUsage(
        input_tokens=usage.input_tokens,
        output_tokens=usage.output_tokens,
        cache_read_tokens=usage.cache_read_tokens,
        cache_write_tokens=usage.cache_write_tokens,
        details={**usage.details, COST_DETAIL_KEY: round(cost_usd * 1_000_000)},
    )


class CostMeter:
    """Turns the ``result``'s CUMULATIVE ``total_cost_usd`` into per-turn
    charges: each ``charge`` bills the increase since the previous result.
    One meter per process — ``reset`` on spawn, because a fresh or resumed
    ``claude`` starts its running total at zero (the CLI documents it so).
    A total that went DOWN on a live process means the CLI reset it (the
    documented mid-session ``/clear``); then the new total IS everything
    spent since the reset, so it is billed whole and becomes the baseline —
    never a negative charge.

    The meter keeps its baseline in the ledger's own unit — micro-USD,
    rounded the way ``charge_cost`` rounds — and bills integer differences.
    Rounding each USD delta on its own would let the ledger's sum drift from
    the CLI's total by a micro-dollar per turn (the live 0.0108757 →
    0.0136693 → 0.0161742 sums to 16,175 that way against a 16,174 total);
    differences of already-rounded totals telescope exactly."""

    def __init__(self) -> None:
        self._billed_micro = 0

    def reset(self) -> None:
        self._billed_micro = 0

    def charge(self, cumulative_usd: float | None) -> float | None:
        """The USD to bill for the result reporting ``cumulative_usd``, or
        None when the result carried no cost."""
        if cumulative_usd is None:
            return None
        total = round(cumulative_usd * 1_000_000)
        delta = total - self._billed_micro if total >= self._billed_micro else total
        self._billed_micro = total
        return delta / 1_000_000

    def behind(self, cumulative_usd: float | None) -> bool:
        """True when ``cumulative_usd`` is older than the baseline: a result
        produced BEFORE the one last billed. Only a turn the CLI ran on its
        own can be served that late (a typed turn went out while it sat
        buffered), and then its spend is already on the ledger — the typed
        turn's delta covered it. Billing it through ``charge`` would read the
        lower total as a reset and bill the whole thing a second time."""
        return cumulative_usd is not None and round(cumulative_usd * 1_000_000) < self._billed_micro


# Claude tool_use -> the single arg worth showing on the activity line. Tools not
# listed render as the bare name. Mirrors the TUI's native label keys.
_ACTIVITY_ARG = {
    "Read": "file_path",
    "Write": "file_path",
    "Edit": "file_path",
    "Bash": "command",
    "Grep": "pattern",
    "Glob": "pattern",
    "WebSearch": "query",
    "WebFetch": "url",
    "Agent": "description",
    "Task": "description",
}


# A narrow, non-emoji marker. ``⏺`` (U+23FA) is emoji-presentation: terminals draw
# it 2 cells wide while rich/Textual lay it out as 1, so the line shifts and a
# character gets clipped at the wrap ("Read" rendered as "Rea"). ``▸`` is a plain
# geometric glyph that renders at its 1-cell width, keeping the layout honest.
_ACTIVITY_MARKER = "▸"


def format_activity_line(name: str, tool_input: dict) -> str:
    """A compact ``▸ <Tool> <summary>`` line for one Claude tool_use, folded into
    the streamed text so the user sees progress (we cannot surface real tool-call
    parts — pydantic_ai would try to execute them)."""
    key = _ACTIVITY_ARG.get(name)
    summary = ""
    if key:
        raw = tool_input.get(key, "")
        summary = " " + str(raw).strip().splitlines()[0] if str(raw).strip() else ""
    return f"{_ACTIVITY_MARKER} {name}{summary}"


@dataclass
class TextChunk:
    """A run of assistant prose (one ``text_delta``)."""

    delta: str


@dataclass
class ThinkingChunk:
    """A run of Claude's thinking (one ``thinking_delta``); rendered as a
    thinking part so the TUI can fold it like a native model's."""

    delta: str


@dataclass
class ToolUseChunk:
    """Claude invoked one of its own tools. Surfaced structurally so the TUI can
    render a native tool card; folded to a ``▸`` text line when there's no UI."""

    name: str  # Claude Code tool name, e.g. "Read"/"Bash"
    tool_input: dict
    call_id: str


@dataclass
class ToolResultChunk:
    """The result of a prior ``ToolUseChunk`` (matched by ``call_id``), so a live
    tool card can flip from pending to done/failed."""

    call_id: str
    content: str
    is_error: bool


@dataclass
class PromptUsageChunk:
    """One ``assistant`` object's ``message.usage``: the size of the request
    Claude just made (cache-inclusive input tokens). The CLI repeats the same
    usage on every ``assistant`` object of one response (one per content
    block), so consumers treat it as a level, not a delta. ``model`` is the
    ``message.model`` that answered — the key under which the result's
    ``modelUsage`` reports its window."""

    prompt_tokens: int
    model: str | None = None


@dataclass
class InitChunk:
    """The turn's ``system/init``: the session id (the resume key, persisted
    as the session ref) and the CLI version (checked against
    ``MIN_CLAUDE_VERSION`` once)."""

    session_id: str | None
    version: str
    model: str


@dataclass
class DoneChunk:
    """Terminal chunk: Claude's session id, usage, and whether a proper
    ``result`` was seen. ``complete=False`` ⇒ the turn failed (``error_detail``
    says why: the CLI's exit code and stderr tail, or the result's error
    subtype). ``aborted`` marks an interrupted turn that still ended cleanly
    with an aborted result (steer/Ctrl-C) — complete, just cut short."""

    session_id: str | None
    usage: RequestUsage
    complete: bool
    error_detail: str = ""
    aborted: bool = False
    # The result's running ``total_cost_usd`` (cumulative over the process —
    # see ``CostMeter``); the usage above carries NO cost until the model
    # bills the per-turn delta.
    cumulative_cost_usd: float | None = None
    # ``modelUsage[<model>].contextWindow`` for the model that did the most
    # input work over the process (the fallback when no request named its
    # model), and the per-model map so the model behind the LAST request
    # can be looked up directly — after a fallback to a smaller-window
    # model the old model's cumulative total keeps it heaviest for a while.
    context_window: int | None = None
    context_windows: dict[str, int] = field(default_factory=dict)


def _flatten_result_content(content) -> str:
    """A tool_result's content (str or list of content blocks) reduced to text."""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "".join(
            b.get("text", "") for b in content if isinstance(b, dict) and b.get("type") == "text"
        )
    return "" if content is None else str(content)


def _is_subagent_noise(obj: dict) -> bool:
    """True when ``obj`` is Claude-side sub-agent traffic — or that sub-agent's
    system-event lifecycle noise — neither of which belongs in the main turn.

    Objects tagged ``parent_tool_use_id`` belong to a Claude-side sub-agent, not
    the main turn; headless there is no demux to route them to, so they are
    dropped — otherwise a child's prose would leak into the main response text.
    ``task_started``/``task_updated``/``task_notification`` system events are the
    same sub-agent's lifecycle noise (the demux path renders it) and are skipped
    too."""
    if obj.get("parent_tool_use_id"):
        return True
    return obj.get("type") == "system" and obj.get("subtype") in (
        "task_started",
        "task_updated",
        "task_notification",
    )


def _delta_chunk(obj: dict) -> TextChunk | ThinkingChunk | None:
    """The chunk for one ``stream_event``, or None for the ones that carry no
    text (block starts/stops, signature deltas, message deltas)."""
    event = obj.get("event") or {}
    if event.get("type") != "content_block_delta":
        return None
    delta = event.get("delta") or {}
    kind = delta.get("type")
    if kind == "text_delta":
        text = delta.get("text") or ""
        return TextChunk(text) if text else None
    if kind == "thinking_delta":
        thinking = delta.get("thinking") or ""
        return ThinkingChunk(thinking) if thinking else None
    return None


def _tool_use_chunks(obj: dict) -> Iterator[ToolUseChunk]:
    """The ``tool_use`` blocks of one ``assistant`` object. Its text/thinking
    blocks are skipped: the deltas already carried them."""
    for block in (obj.get("message") or {}).get("content") or []:
        if block.get("type") == "tool_use":
            yield ToolUseChunk(
                name=block.get("name", "tool"),
                tool_input=block.get("input") or {},
                call_id=block.get("id", ""),
            )


def _user_chunks(obj: dict) -> Iterator:
    """The ``ToolResultChunk``s in one ``user`` stream-json object's content
    blocks."""
    for block in (obj.get("message") or {}).get("content") or []:
        if block.get("type") == "tool_result":
            yield ToolResultChunk(
                call_id=block.get("tool_use_id", ""),
                content=_flatten_result_content(block.get("content")),
                is_error=bool(block.get("is_error")),
            )


def _result_error_subtype(obj: dict) -> str | None:
    """The failure label for a ``result`` event that errored, else None.

    A ``result`` is a failure when it flags ``is_error`` or carries a non-success
    ``subtype`` (e.g. ``error_max_turns``, ``error_during_execution``). The CLI's
    normal terminal result is ``subtype: "success"`` with ``is_error`` absent/false."""
    if obj.get("is_error"):
        return str(obj.get("subtype") or "is_error")
    subtype = obj.get("subtype")
    if isinstance(subtype, str) and subtype and subtype != "success":
        return subtype
    return None


# terminal_reason values of the aborted result an ``interrupt`` control request
# produces (probe s7): a normal, if truncated, end of turn — not a failure.
_ABORTED_REASONS = frozenset({"aborted_tools", "aborted_streaming"})


def _result_chunk(obj: dict, *, produced_text: bool) -> DoneChunk:
    """The ``DoneChunk`` for a turn's ``result``.

    An *errored* result must not masquerade as a clean turn, but an interrupted
    one is not an error: ``is_error`` with an aborted ``terminal_reason`` is how
    the CLI ends a turn marim itself cut short. Otherwise the pre-existing
    policy holds — log the failure subtype; KEEP any prose already streamed
    (partial output beats none) by leaving ``complete=True`` with the error in
    ``error_detail``; with NO usable text the turn is a bare failure and
    ``complete=False`` makes the model raise ``CliModelError``."""
    usage = request_usage_from_cli(obj.get("usage"), None)
    facts = {
        "session_id": obj.get("session_id"),
        "usage": usage,
        "cumulative_cost_usd": _cumulative_cost(obj),
        "context_window": _context_window(obj),
        "context_windows": _context_windows(obj),
    }
    error = _result_error_subtype(obj)
    if error is None:
        return DoneChunk(complete=True, **facts)
    if obj.get("terminal_reason") in _ABORTED_REASONS:
        return DoneChunk(complete=True, aborted=True, **facts)
    logger.warning("claude CLI result reported an error: %s", error)
    return DoneChunk(complete=produced_text, error_detail=f"CLI result error: {error}", **facts)


def _cumulative_cost(obj: dict) -> float | None:
    cost = obj.get("total_cost_usd")
    return float(cost) if isinstance(cost, (int, float)) and not isinstance(cost, bool) else None


def _context_windows(obj: dict) -> dict[str, int]:
    """``model id -> contextWindow`` from the result's ``modelUsage`` (one
    entry per model the process has used); entries without a usable window
    are left out, and a ``modelUsage`` that is not a mapping at all reads as
    empty (the window is a nicety; a malformed one must not fail the turn)."""
    out: dict[str, int] = {}
    usage = obj.get("modelUsage")
    for model, entry in usage.items() if isinstance(usage, dict) else ():
        if not isinstance(entry, dict):
            continue
        window = entry.get("contextWindow")
        if isinstance(window, bool) or not isinstance(window, int) or window <= 0:
            continue
        out[str(model)] = window
    return out


def _context_window(obj: dict) -> int | None:
    """The context window of the model that did the turn, from the result's
    ``modelUsage`` (cumulative per model over the process). A turn can
    involve several models (a haiku sub-task under a sonnet main loop), so
    the entry with the most input tokens is taken as the main loop's; None
    when the result carries no usable window. Only a fallback: the adapter
    prefers the window of the model its last request named."""
    best: tuple[int, int] | None = None
    windows = _context_windows(obj)  # only models with a usable window qualify
    for model, window in windows.items():
        weight = _input_weight(obj["modelUsage"][model])
        if best is None or weight > best[0]:
            best = (weight, window)
    return best[1] if best else None


def _input_weight(entry: dict) -> int:
    """How much input a ``modelUsage`` entry has done (uncached + cache
    reads); junk buckets weigh nothing."""
    total = 0
    for k in ("inputTokens", "cacheReadInputTokens"):
        v = entry.get(k)
        if isinstance(v, (int, float)) and not isinstance(v, bool):
            total += int(v)
    return total


def _closed_detail(obj: dict) -> str:
    """The failure text for a turn that ended with the process closing: the
    exit code plus the stderr tail the process captured (a stack tail, "Invalid
    API key", "No conversation found with session ID …")."""
    stderr = str(obj.get("stderr") or "").strip()
    return f"claude exited (code {obj.get('returncode')}): {stderr}"


def _event_chunks(obj: dict) -> Iterator:
    """The chunks for one NON-terminal turn object. A plain (sync) generator so
    ``consume_cli_stream`` spends one branch on the whole mid-turn vocabulary
    instead of an ``elif`` per object kind — which is what keeps that async
    generator (which cannot ``yield from``) under the complexity ceiling."""
    kind = obj.get("type")
    if kind == "system":
        # Every other system subtype (status, thinking_tokens, the sub-agent
        # lifecycle noise `_is_subagent_noise` already dropped) carries nothing
        # the transcript needs.
        if obj.get("subtype") == "init":
            yield InitChunk(
                session_id=obj.get("session_id") or None,
                version=str(obj.get("claude_code_version") or ""),
                model=str(obj.get("model") or ""),
            )
    elif kind == "stream_event":
        chunk = _delta_chunk(obj)
        if chunk is not None:
            yield chunk
    elif kind == "assistant":
        message = obj.get("message") or {}
        usage = message.get("usage")
        if isinstance(usage, dict):
            yield PromptUsageChunk(prompt_tokens(usage), str(message.get("model") or "") or None)
        yield from _tool_use_chunks(obj)
    elif kind == "user" and not obj.get("isReplay"):
        # `isReplay` is the CLI echoing back a message marim itself sent (the
        # turn's prompt, or a steer) — never a tool result.
        yield from _user_chunks(obj)


async def consume_cli_stream(objs: AsyncIterator[dict]) -> AsyncGenerator:
    """One turn's stream-json objects → structured chunks, ending with exactly
    one ``DoneChunk``.

    Prose/thinking come from ``stream_event`` deltas (``TextChunk`` /
    ``ThinkingChunk``); ``assistant`` objects add a ``PromptUsageChunk`` (the
    request's size, for the context report) and ``ToolUseChunk``s; ``user``
    objects add ``ToolResultChunk``s (``isReplay`` echoes of our own messages
    are dropped); ``system/init`` becomes an ``InitChunk``; every other
    ``system`` subtype (status, thinking_tokens, the sub-agent lifecycle noise)
    is skipped. Objects tagged ``parent_tool_use_id`` belong to a Claude-side
    sub-agent: with a UI the demux tee consumed them before we see them;
    headless they are dropped here so a child's prose never leaks into the main
    text.

    The turn ends at its ``result`` (one per turn on the bidirectional
    transport — the process layer closes the turn there) or at the synthetic
    ``CLOSED`` object the process publishes when the CLI exits mid-turn."""
    produced_text = False
    async for obj in objs:
        if _is_subagent_noise(obj):
            continue
        kind = obj.get("type")
        if kind == CLOSED:
            yield DoneChunk(
                session_id=None,
                usage=RequestUsage(),
                complete=False,
                error_detail=_closed_detail(obj),
            )
            return
        if kind == "result":
            yield _result_chunk(obj, produced_text=produced_text)
            return
        for chunk in _event_chunks(obj):
            if isinstance(chunk, TextChunk):
                produced_text = True
            yield chunk
    yield DoneChunk(session_id=None, usage=RequestUsage(), complete=False)


def fold_chunk_text(chunk, *, leading: bool) -> str:
    """The text representation of a visible chunk for the headless (no-UI) paths:
    assistant prose as-is, a ``ToolUseChunk`` as its ``▸`` activity line. Segments
    are blank-line separated (``leading`` is True only for the first one).
    ``ToolResultChunk``/``DoneChunk`` contribute no text. Returns ``""`` to skip."""
    if isinstance(chunk, TextChunk):
        segment = chunk.delta
    elif isinstance(chunk, ToolUseChunk):
        segment = format_activity_line(chunk.name, chunk.tool_input)
    else:
        return ""
    return segment if leading else f"\n\n{segment}"


def cli_activity_events(chunk) -> list:
    """Translate a tool chunk into display-only pydantic-ai stream events for the
    TUI side-channel — a ``FunctionToolCallEvent`` for a use, a
    ``FunctionToolResultEvent`` for a result. Tool names/args are normalized to the
    harness shapes (Read→read_file, …) so they render as native tool cards. These
    NEVER enter the model response, so pydantic_ai never executes them."""
    from datetime import datetime, timezone

    from pydantic_ai.messages import (
        FunctionToolCallEvent,
        FunctionToolResultEvent,
        ToolCallPart,
        ToolReturnPart,
    )

    from ..subagents.cli_backend import normalize_cc_tool

    if isinstance(chunk, ToolUseChunk):
        name, args = normalize_cc_tool(chunk.name, chunk.tool_input)
        return [
            FunctionToolCallEvent(
                part=ToolCallPart(tool_name=name, args=args, tool_call_id=chunk.call_id)
            )
        ]
    if isinstance(chunk, ToolResultChunk):
        return [
            FunctionToolResultEvent(
                part=ToolReturnPart(
                    tool_name="tool",
                    content=chunk.content,
                    tool_call_id=chunk.call_id,
                    timestamp=datetime.now(tz=timezone.utc),
                    outcome="failed" if chunk.is_error else "success",
                )
            )
        ]
    return []


class _FoldedText:
    """The headless (``request()``) counterpart of ``TextFolder``'s fold mode.

    Prose arrives one ``text_delta`` at a time — a few characters each — so the
    blank line ``fold_chunk_text`` puts between *segments* must separate a ``▸``
    tool line from the prose around it, never one delta from the next (that
    would shred every sentence). Only a folded tool line arms the separator."""

    def __init__(self) -> None:
        self._parts: list[str] = []
        self._after_tool = False

    def add(self, chunk) -> None:
        if isinstance(chunk, TextChunk):
            self._parts.append(f"\n\n{chunk.delta}" if self._after_tool else chunk.delta)
            self._after_tool = False
            return
        segment = fold_chunk_text(chunk, leading=not self._parts)
        if segment:
            self._parts.append(segment)
            self._after_tool = True

    def text(self) -> str:
        return "".join(self._parts)


def _no_result_message(done: DoneChunk | None) -> str:
    """The error text for a turn that ended without a proper ``result`` event.
    Appends the CLI's stderr tail (captured on the terminal ``DoneChunk``) when
    present so a crash / not-logged-in / bad-flag failure carries a real
    diagnostic instead of a bare, undebuggable line."""
    base = "claude produced no result (crash or bad output)"
    detail = done.error_detail if done is not None else ""
    return f"{base}: {detail}" if detail else f"{base}."


def _version_tuple(version: str) -> tuple[int, ...]:
    """``"2.1.261"`` → ``(2, 1, 261)``; non-numeric segments end the tuple so a
    ``2.1.0-beta`` compares as ``(2, 1, 0)``."""
    out: list[int] = []
    for piece in version.split("."):
        digits = "".join(ch for ch in piece if ch.isdigit())
        if not digits:
            break
        out.append(int(digits))
    return tuple(out)


_version_warned = False


def note_old_version_once(version: str) -> None:
    """Warn once per process when the CLI predates the protocol this provider
    was built against (``MIN_CLAUDE_VERSION``). The turn still runs — the check
    is a hint for the "why does approval never prompt?" support question, not
    a gate (spec §Version check)."""
    global _version_warned
    if _version_warned or not version:
        return
    if _version_tuple(version) < _version_tuple(MIN_CLAUDE_VERSION):
        _version_warned = True
        logger.warning(
            "claude %s is older than %s; marim's approval/steer integration may not work. "
            "Update Claude Code (`claude update`).",
            version,
            MIN_CLAUDE_VERSION,
        )


def _turn_stream(
    process: ClaudeProcess, handle: TurnHandle, first: dict
) -> AsyncGenerator[dict, None]:
    """``turn_objects`` for one turn, typed as the async *generator* it is.

    Its declared ``AsyncIterator`` return type hides ``aclose()``, which both
    entry points below must call in their ``finally`` so an abandoned or
    cancelled turn is finalized deterministically instead of at GC time."""
    return cast("AsyncGenerator[dict, None]", turn_objects(process, handle, first))


def _log_steer_failure(task: asyncio.Task) -> None:
    """Retrieve a fire-and-forget steer's exception so asyncio does not report
    it as never-retrieved, and leave a trace: the send can fail (the process
    died between the turn_open check and the write) and the user sees only
    that their steer went nowhere."""
    if task.cancelled():
        return
    exc = task.exception()
    if exc is not None:
        logger.debug("claude steer failed", exc_info=exc)


def backend_turn_note(opening: list[dict]) -> str:
    """The turn-context note for marim's autonomous turn over a turn Claude
    Code ran on its own, from the objects that turn opened with. It names the
    sub-agent reports (``task_notification``: status + summary) the CLI reacted
    to, so the persisted marim history says why an assistant message follows
    a turn nobody typed; Claude itself never sees it (its own history already
    carries the notification)."""
    reports = [
        obj
        for obj in opening
        if obj.get("type") == "system" and obj.get("subtype") == "task_notification"
    ]
    if not reports:
        return (
            "[Claude Code ran a turn of its own (a background sub-agent reported "
            "back); its response follows.]"
        )
    lines = []
    for obj in reports:
        status = str(obj.get("status") or "completed")
        summary = " ".join(str(obj.get("summary") or "").split()) or "(no summary)"
        lines.append(f"sub-agent {obj.get('tool_use_id') or '?'} {status}: {summary}")
    return (
        "[background sub-agent reports Claude Code reacted to on its own; "
        "its response follows:\n" + "\n".join(lines) + "]"
    )


def _is_autonomous(messages: list[ModelMessage]) -> bool:
    """True for a turn nobody typed: the newest prompt is a turn-context
    envelope around an empty typed text (the harness's autonomous / digest
    turns) — the kind that consumes a turn the CLI already ran."""
    # An image-only submission is still a user turn. Treating its empty text
    # as autonomous would consume a buffered CLI response and drop the image.
    return all(
        isinstance(item, str) and not strip_turn_context(item).strip()
        for item in prompt_content(messages, history=False)
    )


def _is_missing_session(obj: dict) -> bool:
    """True when a ``--resume`` start died because the CLI no longer has the
    session (probe s8: stderr ``No conversation found with session ID: …``,
    exit 1) — the one CLOSED the model recovers from by starting fresh."""
    return obj.get("type") == CLOSED and "No conversation found" in str(obj.get("stderr") or "")


class ClaudeCliModel(ExternalCliModel):
    """A Pydantic AI model backed by one long-lived ``claude`` process.

    The late-bound seams (``mode_getter``, ``cwd``, ``request_approval``,
    ``ask_user``, ``on_activity``, ``on_subagent*``, ``scratchpad_getter``,
    ``session_ref_getter``/``on_session_ref``) live on ``ExternalCliModel`` and
    are bound by ``Harness.wire_cli_model``. The approval broker snapshots the
    UI seams when the process starts; ``bind_ui`` after the first turn is
    picked up by the next process (a documented residual)."""

    provider_id = "claude-cli"

    def __init__(self, model_id: str | None, *, ephemeral: bool = False) -> None:
        super().__init__()
        self._model_id = model_id
        # See ExternalCliModel.ephemeral / ``ephemeral_clone``: aux agents never
        # resume or store a session, so they can't hijack the user's live one.
        self.ephemeral = ephemeral
        self._process: ClaudeProcess | None = None
        # Clones made by ephemeral_clone(); closed with their parent, because
        # nothing else holds them (Harness.aclose knows only the session's
        # current model, and the aux titler/summarizer/advisor keep theirs
        # inside a pydantic-ai Agent). Mirrors CodexCliModel._clones.
        self._clones: list[ClaudeCliModel] = []
        self._broker: ClaudeApprovalBroker | None = None
        # What Claude reports about its own context and subscription, for the
        # status bar / GET session (read the way codex-cli's quota_hint is:
        # `getattr(model, ...)`). Refreshed per assistant event / per result
        # / once per turn respectively; None until the first reading.
        self.context_report: ContextReport | None = None
        self.quota_hint: QuotaHint | None = None
        # The window is a per-model constant learned at each result; kept
        # apart so the next turn's first reading carries it before its own
        # result arrives. ``_prompt_model`` is the model the last request
        # named, so the result's per-model windows can be read for IT rather
        # than for whichever model has done the most work over the process.
        self._context_window: int | None = None
        self._prompt_model: str | None = None
        self._cost = CostMeter()
        # The sub-agent demux lives as long as the process: a background
        # spawn's card is opened in one turn and settled by a notification
        # that can arrive turns later (in the CLI's own turn on it).
        self._demux: CliSubagentDemux | None = None

    def ephemeral_clone(self, *, cwd: str) -> ClaudeCliModel:
        """A stateless, read-only copy for one-shot aux agents (titler/summarizer).

        It never resumes or stores a Claude session — so titling/summarizing can't
        continue or hijack the user's live conversation — always sends its own
        instructions, runs in plan (read-only) mode so it can't edit files, and
        closes its process after every call."""
        clone = ClaudeCliModel(self._model_id, ephemeral=True)
        clone.cwd = cwd
        clone.mode_getter = lambda: "plan"
        self._clones.append(clone)
        return clone

    @property
    def model_name(self) -> str:
        return self._model_id or "default"

    @property
    def session_id(self) -> str | None:
        """The live process's session id, else the persisted one (the key the
        next process resumes with)."""
        if self._process is not None and self._process.session_id:
            return self._process.session_id
        return self._persisted_session_id()

    # --- collaborators ------------------------------------------------------------
    def _mode(self) -> Mode:
        raw = self.mode_getter() if self.mode_getter is not None else "plan"
        try:
            return Mode(raw)
        except ValueError:
            return Mode.plan

    def _scratchpad(self) -> Path | None:
        return self.scratchpad_getter() if self.scratchpad_getter is not None else None

    def _make_broker(self) -> ClaudeApprovalBroker:
        return ClaudeApprovalBroker(
            mode_getter=self._mode,
            workspace_root=Path(self.cwd),
            scratchpad_getter=self._scratchpad,
            ui=UiSeams(request_approval=self.request_approval, ask_user=self.ask_user),
        )

    def _persisted_session_id(self) -> str | None:
        if self.ephemeral or self.session_ref_getter is None:
            return None
        ref = self.session_ref_getter()
        if not ref or not ref.startswith(SESSION_REF_PREFIX):
            return None  # another provider's ref (e.g. codex-cli) — ignore
        return ref[len(SESSION_REF_PREFIX) :] or None

    def _options(self, *, resume_id: str | None, system: str | None) -> ProcessOptions:
        binary = resolve_cli_binary()
        if binary is None:
            raise CliModelError(f"claude CLI not found. {INSTALL_HINT}")
        return ProcessOptions(
            binary=binary,
            cwd=self.cwd,
            model=self._model_id or None,
            resume_id=resume_id,
            append_system=system,
            persist=not self.ephemeral,
        )

    # --- process lifecycle --------------------------------------------------------
    async def _spawn(self, *, resume_id: str | None, system: str | None) -> ClaudeProcess:
        # Imported here, not at module top: cli_demux imports cli_backend,
        # which imports this module's chunk types.
        from ..subagents.cli_demux import CliSubagentDemux

        self._broker = self._make_broker()
        process = ClaudeProcess(
            self._options(resume_id=resume_id, system=system),
            on_request=self._broker.handle,
            silence_timeout=cli_timeout(),
            # Aux clones close after every call, so they never idle.
            idle_timeout=0.0 if self.ephemeral else cli_idle_timeout(),
            on_unsolicited=self._on_unsolicited,
        )
        await process.start()
        self._process = process
        self._demux = CliSubagentDemux()
        # A new process — fresh or resumed — restarts the CLI's running cost
        # total at zero; the meter must follow or the first result is billed
        # against the old process's total.
        self._cost.reset()
        return process

    def _on_unsolicited(self, opening: list[dict]) -> None:
        """The process saw Claude open a turn of its own: tell the harness so
        it queues the autonomous marim turn that consumes it (`_open_turn`).
        Unbound (headless one-shot, an aux clone) the turn stays buffered:
        the next turn marim sends waits it out, and its text shows nowhere in
        marim — Claude's own history keeps it, so the conversation itself
        stays coherent."""
        if self.on_backend_turn is not None:
            self.on_backend_turn(backend_turn_note(opening))

    async def _ensure_process(self, messages: list) -> tuple[ClaudeProcess, bool]:
        """The process to run this turn on and whether it RESUMES a Claude
        session (so the turn sends only the newest user text). Order: the live
        process; a respawn on a dead process's session id (idle close, crash,
        an ignored interrupt); the persisted session ref; a cold start carrying
        the flattened history and the system prompt."""
        process = self._process
        if process is not None:
            # The idle reaper may be inside aclose() right now: wait it out
            # rather than race it (cancelling a close half-done leaves a
            # process that answers nothing), then fall through and respawn on
            # its session id.
            await process.wait_closing()
        if process is not None and process.alive:
            return process, True
        resume_id = process.session_id if process is not None else self._persisted_session_id()
        if resume_id:
            return await self._spawn(resume_id=resume_id, system=None), True
        return await self._spawn(resume_id=None, system=extract_system(messages) or None), False

    async def _start_turn(
        self, messages: list, model_settings: ModelSettings | None
    ) -> tuple[ClaudeProcess, TurnHandle, dict]:
        """Send the turn and pull its first object, so a resume of a session the
        CLI no longer has (probe s8) is caught here and retried as a cold start
        — the caller then streams the rest uniformly."""
        try:
            return await self._open_turn(messages, model_settings)
        except BaseException:
            # This runs BEFORE request()/request_stream()'s try/finally, so
            # nothing there can clean up after a failure here (a silence
            # timeout, a spawn error, a cancel). A long-lived model keeps its
            # process on purpose — aclose() still reaches it and the next turn
            # resumes on it — but an ephemeral clone's process is owned by this
            # one call: leave it running and the next aux call resumes the leak.
            if self.ephemeral and self._process is not None:
                await self._process.aclose()
                self._process = None
            raise

    async def _open_turn(
        self, messages: list, model_settings: ModelSettings | None
    ) -> tuple[ClaudeProcess, TurnHandle, dict]:
        process, resumed = await self._ensure_process(messages)
        # An autonomous turn exists to show a turn Claude already ran on its
        # own (a background sub-agent's report): serve it from the buffered
        # handle and send nothing — the CLI's history already moved on, and
        # the controls below govern what marim SENDS, so they wait for the
        # next sent turn. A typed turn leaves the buffer alone; the
        # autonomous turn queued for it runs next. (An autonomous turn with
        # nothing buffered — a finished-jobs digest — is sent like any other.)
        buffered = process.take_unsolicited() if _is_autonomous(messages) else None
        if buffered is not None:
            return process, buffered, await next_turn_object(process, buffered)
        await self._sync_controls(process, model_settings)
        content = claude_input(prompt_content(messages, history=not resumed))
        logger.debug(
            "claude turn input: images=%d, replay_history=%s",
            sum(block["type"] == "image" for block in content),
            not resumed,
        )
        handle = await process.send_turn(content)
        first = await next_turn_object(process, handle)
        if resumed and _is_missing_session(first):
            logger.warning(
                "claude session %s is gone; starting a fresh one from the flattened history",
                process.session_id,
            )
            await process.aclose()
            process = await self._spawn(resume_id=None, system=extract_system(messages) or None)
            await self._sync_controls(process, model_settings)
            handle = await process.send_turn(claude_input(prompt_content(messages, history=True)))
            first = await next_turn_object(process, handle)
        return process, handle, first

    # --- control sync -------------------------------------------------------------
    def _thinking(self, model_settings: ModelSettings | None) -> str | None:
        """The turn's thinking level: the per-turn ``ModelSettings`` (the main
        loop, via ``TurnController._turn_model_settings``) first, else the live
        harness level — the same precedence as codex-cli."""
        level = (model_settings or {}).get("thinking")
        if level is None and self.thinking_getter is not None:
            level = self.thinking_getter()
        return str(level) if level else None

    async def _sync_controls(
        self, process: ClaudeProcess, model_settings: ModelSettings | None
    ) -> None:
        """Bring the process's permission mode, model and thinking in line
        with marim's before the turn goes out — only what differs from what
        the process last acknowledged (``ClaudeProcess.controls``), so an
        unchanged session costs nothing and a fresh process gets each once.

        Aux clones are skipped: they run one read-only call each, the broker's
        denial already keeps them read-only, and Claude's plan-mode system
        prompt ("research, then present a plan") would leak into a title or a
        summary."""
        if self.ephemeral:
            return
        state = process.controls
        mode = self._mode()
        if state.mode != mode:
            await self._apply_control("mode", process.set_mode(mode))
        model = self._model_id or None
        if state.model != model:
            await self._apply_control("model", process.set_model(model))
        thinking = thinking_controls(self._thinking(model_settings))
        if state.thinking != thinking:
            await self._apply_control("thinking", process.set_thinking(thinking))

    async def _apply_control(self, what: str, sending: Awaitable[None]) -> None:
        """One control send with the failure policy per lever. A closed
        process is left for ``send_turn`` to report (its handle delivers
        CLOSED with the exit detail). A rejected or unanswered MODEL fails
        the turn: running it on a model the user did not pick is worse than
        no turn, and the error names what to fix. A rejected mode or
        thinking is logged and the turn goes on — marim's own broker still
        enforces the mode, and thinking is best-effort by contract."""
        try:
            await sending
        except ProcessClosed:
            return
        except (ControlError, asyncio.TimeoutError) as exc:
            detail = str(exc) or type(exc).__name__
            if what == "model":
                raise CliModelError(f"claude did not accept the model switch: {detail}") from exc
            logger.warning(
                "claude ignored the %s switch (%s); marim's own gate still applies", what, detail
            )

    def adopt(self, previous: Model) -> None:
        """Take over ``previous``'s live process on a same-provider ``/model``
        switch, so the switch is one ``set_model`` on the next turn instead of
        a close + ``--resume`` respawn (which re-reads the session and pays a
        cold prompt-cache). Everything that describes the process moves with
        it — the broker it answers to, the context/quota readings, the cost
        baseline (the CLI's running total keeps counting on the same process)
        — and is cleared on ``previous`` so the ``aclose()`` the harness
        schedules for it closes nothing. Aux clones stay with ``previous``
        (their agents are rebuilt on the new model). Ephemeral models never
        take part: an aux clone's process is owned by one call."""
        if not isinstance(previous, ClaudeCliModel) or previous is self:
            return
        if self.ephemeral or previous.ephemeral or previous._process is None:
            return
        self._process, previous._process = previous._process, None
        self._broker, previous._broker = previous._broker, None
        self._cost, previous._cost = previous._cost, CostMeter()
        self.context_report, previous.context_report = previous.context_report, None
        self.quota_hint, previous.quota_hint = previous.quota_hint, None
        self._context_window, self._prompt_model = previous._context_window, previous._prompt_model

    def release_conversation(self) -> None:
        """Drop the live process (see the base): the store was rebound, so the
        process — and everything read off it: the broker, the context/quota
        readings, the cost baseline — describes a conversation this session
        no longer is. The close is scheduled, not awaited (the harness's
        switch path is sync); without a running loop there is nothing alive
        to close (a process only exists inside the loop that spawned it)."""
        process, self._process = self._process, None
        self._broker = None
        self.context_report = self.quota_hint = None
        self._context_window = self._prompt_model = None
        self._cost = CostMeter()
        if process is None:
            return
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return
        task = loop.create_task(process.aclose())
        task.add_done_callback(lambda t: t.exception() if not t.cancelled() else None)

    async def _after_turn(self, process: ClaudeProcess, handle: TurnHandle) -> None:
        """Every exit path of a turn: a turn still open (the consumer abandoned
        or cancelled the stream) is interrupted so Claude stops working on an
        answer nobody reads; an ephemeral clone's process is closed outright."""
        if handle.open and process.alive:
            await process.interrupt(handle)
        if self.ephemeral:
            await process.aclose()
            self._process = None

    def _note(self, chunk: InitChunk | PromptUsageChunk) -> None:
        """A mid-turn chunk the model itself keeps state from: the init's
        session id / version, and each request's prompt size (the live
        context report, window carried over from the last result)."""
        if isinstance(chunk, PromptUsageChunk):
            if chunk.model and chunk.model != self._prompt_model:
                # A model change (a fallback, a /model switch) invalidates
                # the carried window until the result names the new one.
                self._prompt_model, self._context_window = chunk.model, None
            self.context_report = ContextReport(chunk.prompt_tokens, self._context_window)
            return
        note_old_version_once(chunk.version)
        if chunk.session_id and not self.ephemeral and self.on_session_ref is not None:
            self.on_session_ref(SESSION_REF_PREFIX + chunk.session_id)

    def _settle_turn(self, done: DoneChunk, *, own: bool = False) -> RequestUsage:
        """The turn's usage with its per-turn cost billed (``CostMeter``), and
        the context report's window refreshed from the result.

        Deliberately NOT called for a turn that raises (a ``result`` that
        errored with no usable text): the raise records no usage anywhere, so
        advancing the meter there would drop that turn's spend from the
        ledger for good. Leaving the baseline where it was makes the next
        successful turn carry the failed one's cost — attributed late, but
        the session total stays equal to the CLI's own running total, which
        is the number the ledger is meant to reproduce.

        ``own`` marks a turn the CLI ran by itself, served from the buffer.
        Played after a typed turn that went out first, its result is older
        than the last one billed: the cost is on the ledger already and the
        window is staler than the one shown, so both are left alone."""
        if own and self._cost.behind(done.cumulative_cost_usd):
            return charge_cost(done.usage, 0.0)
        window = done.context_windows.get(self._prompt_model or "") or done.context_window
        if window:
            self._context_window = window
            if self.context_report is not None:
                self.context_report = self.context_report.with_window(window)
        return charge_cost(done.usage, self._cost.charge(done.cumulative_cost_usd))

    def _response_details(self) -> dict | None:
        """``provider_details`` for the turn's response: the context report,
        persisted so a resumed session shows Claude's last known numbers
        before its first new turn (``context_report.last_context_report``)."""
        if self.context_report is None:
            return None
        return {CONTEXT_REPORT_KEY: self.context_report.to_payload()}

    async def _refresh_quota(self, process: ClaudeProcess) -> None:
        """Poll ``get_usage`` once, after the turn's result (on the consuming
        task's normal await path, never inside a cancellation-time
        ``finally``), and keep the reading for the status bar. Best-effort:
        the hint is informational, and an aux clone (whose process is about
        to close) is skipped outright. A failed poll CLEARS the hint rather
        than leaving the previous reading up: the status bar renders any
        hint it has, and a number nobody refreshed is a stale claim about a
        quota that keeps moving."""
        if self.ephemeral or not process.alive:
            return
        try:
            self.quota_hint = quota_from_usage(await process.read_usage())
        except Exception as exc:  # noqa: BLE001 - best-effort status-line hint
            logger.debug("claude get_usage failed: %s", exc)
            self.quota_hint = None

    # --- pydantic-ai entry points --------------------------------------------------
    async def request(
        self,
        messages: list,
        model_settings: ModelSettings | None,
        model_request_parameters: ModelRequestParameters,
    ) -> ModelResponse:
        process, handle, first = await self._start_turn(messages, model_settings)
        done: DoneChunk | None = None
        folded = _FoldedText()  # assistant prose + folded ▸ tool lines (no UI here)
        objs = _turn_stream(process, handle, first)
        try:
            async with aclosing(consume_cli_stream(objs)) as stream:
                async for chunk in stream:
                    if isinstance(chunk, (InitChunk, PromptUsageChunk)):
                        self._note(chunk)
                    elif isinstance(chunk, DoneChunk):
                        done = chunk
                    else:
                        folded.add(chunk)
            if done is not None and done.complete:
                await self._refresh_quota(process)
        finally:
            await objs.aclose()
            await self._after_turn(process, handle)
        if done is None or not done.complete:
            raise CliModelError(_no_result_message(done))
        return ModelResponse(
            parts=[TextPart(content=folded.text())],
            model_name=self.model_name,
            # Stamp each response with the current time — not a shared
            # construction-time value — so a multi-turn history doesn't carry
            # identical, stale timestamps across every ModelResponse.
            timestamp=datetime.now(tz=timezone.utc),
            usage=self._settle_turn(done, own=handle.own),
            provider_name="claude-cli",
            provider_details=self._response_details(),
        )

    @asynccontextmanager
    async def request_stream(
        self,
        messages: list,
        model_settings: ModelSettings | None,
        model_request_parameters: ModelRequestParameters,
        run_context=None,
    ) -> AsyncGenerator[StreamedResponse]:
        process, handle, first = await self._start_turn(messages, model_settings)
        objs = _turn_stream(process, handle, first)
        stream = ClaudeCliStreamedResponse(
            model_request_parameters=model_request_parameters,
            _objs=objs,
            _model_id=self.model_name,
            # Per-response timestamp (see request()): stamped when the stream is
            # opened, not once at model construction.
            _ts=datetime.now(tz=timezone.utc),
            _on_note=self._note,
            _finish=lambda done: self._settle_turn(done, own=handle.own),
            _details=self._response_details,
            _after=lambda: self._refresh_quota(process),
            _on_activity=self.on_activity,
            _on_subagent=self.on_subagent,
            _on_subagent_model=self.on_subagent_model,
            _demux=self._demux,
        )
        try:
            yield stream
        finally:
            # Deterministic on every exit path — normal completion, error, or
            # Ctrl-C mid-turn: finish the turn iterator, then interrupt the turn
            # if it is still open (the process itself stays for the next turn).
            await objs.aclose()
            await self._after_turn(process, handle)

    # --- live controls -------------------------------------------------------------
    def steer(self, text: str, attachments: list[tuple[bytes, str]] | None = None) -> bool:
        """Fold ``text`` into the open turn (a mid-turn user message — probe s6).
        Fire-and-forget on the running loop: the harness calls this
        synchronously from the input path. False (harness keeps buffering) when
        no turn is open."""
        process = self._process
        if process is None or not process.turn_open:
            return False
        content = claude_input(attachment_content(text, attachments))
        task = asyncio.get_running_loop().create_task(process.send_user(content))
        task.add_done_callback(_log_steer_failure)
        return True

    async def aclose(self) -> None:
        """Close the process. The session id survives on it, so a later turn on
        this model resumes; ``Harness.set_model`` calls this on the outgoing
        model and ``Harness.aclose`` on teardown. Ephemeral clones go first:
        nothing else holds one, and a clone that failed mid-start would
        otherwise keep a ``claude`` process alive for the whole run."""
        for clone in self._clones:
            await clone.aclose()
        self._clones.clear()
        if self._process is not None:
            await self._process.aclose()


# Moved to config/external_cli.py (shared with codex-cli); the old name stays
# importable for tests that reach for it.
_TextFolder = TextFolder


class _ThinkingParts:
    """Vendor-part-id bookkeeping for thinking deltas: one thinking part per
    contiguous run, a fresh id once prose or a tool card intervened — the same
    interleaving rule ``TextFolder`` applies to text."""

    def __init__(self, parts_manager) -> None:
        self._parts_manager = parts_manager
        self._n = 0
        self._open = False

    def close(self) -> None:
        self._open = False

    def emit(self, delta: str):
        part_id = f"think-{self._n}"
        if not self._open:
            self._n += 1
            self._open = True
            part_id = f"think-{self._n}"
            # Same bootstrap as TextFolder._emit: a brand-new vendor_part_id's
            # first handle_thinking_delta is reported as a PartStartEvent, never
            # a PartDeltaEvent, so a consumer that accumulates only deltas would
            # drop the run's first chunk. Open the part with an empty delta and
            # let the real content arrive as a proper delta below.
            yield from self._parts_manager.handle_thinking_delta(vendor_part_id=part_id, content="")
        yield from self._parts_manager.handle_thinking_delta(vendor_part_id=part_id, content=delta)


@dataclass
class ClaudeCliStreamedResponse(StreamedResponse):
    """Streams ``consume_cli_stream`` output as text/thinking-delta events plus
    out-of-band tool cards (``_on_activity``), folding ``▸`` lines instead when
    no UI is bound."""

    _objs: AsyncIterator[dict] | None = None
    _model_id: str = "default"
    _ts: datetime | None = None
    # The model's own bookkeeping seams: init/prompt-usage chunks as they
    # stream (session id, live context report); the DoneChunk → usage
    # settle (per-turn cost); the provider_details to persist; and the
    # once-per-turn quota poll awaited after the settle (best-effort, never
    # raises into the stream — mirrors CodexStreamedResponse._after).
    _on_note: Callable[[InitChunk | PromptUsageChunk], None] | None = None
    _finish: Callable[[DoneChunk], RequestUsage] | None = None
    _details: Callable[[], dict | None] | None = None
    _after: Callable[[], Awaitable[None]] | None = None
    _on_activity: Callable[[list], Awaitable[None]] | None = None
    _on_subagent: Callable[[str, object, object], Awaitable[None]] | None = None
    _on_subagent_model: Callable[[str, str], Awaitable[None]] | None = None
    # The model's process-lifetime demux (None → one for this turn only).
    _demux: CliSubagentDemux | None = None

    async def _demuxed_objs(
        self, activity: Callable[[list], Awaitable[None]] | None, folder: TextFolder
    ) -> AsyncIterator[dict]:
        """Tee the raw stream through a CliSubagentDemux: Claude-side sub-agent
        traffic is delivered out-of-band (the synthesized spawn_agent call/
        return via ``activity`` — the ledger-recording wrap of _on_activity,
        so the spawn persists like any other tool; the top-level sink claims
        those and builds the live card — and child events via _on_subagent,
        keyed by the spawn's tool_use id); everything else flows on to the
        chunk pipeline. A spawn call also bumps the folder's part like a
        ToolUseChunk would, so the prose after it starts a fresh text part
        below the card (live and in the persisted split alike).

        The demux is the model's, shared across the process's turns: a
        background spawn's ``task_notification`` settles a card opened in an
        earlier turn (its return then rides in THIS turn's ledger; the
        persisted expansion drops a return with no call in the same response,
        so the earlier turn keeps its synthesized launch note).

        ``stream_event`` objects bypass the demux: it only knows whole
        assistant/user messages. The main turn's deltas pass straight through;
        a child's (tagged ``parent_tool_use_id``) are dropped — the child's
        whole assistant message reaches its card via the demux anyway."""
        from ..subagents.cli_demux import CliSubagentDemux

        demux = self._demux or CliSubagentDemux()
        assert self._objs is not None
        async for obj in self._objs:
            if obj.get("type") == "stream_event":
                if not obj.get("parent_tool_use_id"):
                    yield obj
                continue
            routed, remainder = demux.route(obj)
            for r in routed:
                await self._deliver_routed(r, activity, folder)
            if remainder is not None:
                yield remainder

    async def _deliver_routed(
        self, r, activity: Callable[[list], Awaitable[None]] | None, folder: TextFolder
    ) -> None:
        """One demux-routed event to its sink (see ``_demuxed_objs``)."""
        if r.stream_id is None:
            if activity is not None:
                await activity([r.event])
                if isinstance(r.event, FunctionToolCallEvent):
                    folder.part_n += 1
        elif self._on_subagent is not None:
            if r.model and self._on_subagent_model is not None:
                await self._on_subagent_model(r.stream_id, r.model)
            await self._on_subagent(r.stream_id, r.event, r.usage)

    def _finalize_done(self, done: DoneChunk | None) -> None:
        """Mirror ``request()``: a stream that ends without a proper ``result``
        (Claude died / produced no result) is a FAILED turn — raise so the
        harness flushes its resumable baseline (clean failure). Otherwise
        settle the usage (per-turn cost) and attach the context report to
        the response's provider_details (merged, next to the activity
        ledger) so it persists with the turn."""
        if done is None or not done.complete:
            raise CliModelError(_no_result_message(done))
        self._usage = self._finish(done) if self._finish is not None else done.usage
        details = self._details() if self._details is not None else None
        if details:
            self.provider_details = {**(self.provider_details or {}), **details}
        self._finished = True

    async def _events_for(self, chunk, folder: TextFolder, thinking: _ThinkingParts):
        """The pydantic-ai events for one non-terminal chunk."""
        if isinstance(chunk, TextChunk):
            thinking.close()
            async for ev in folder.emit_text(chunk.delta):
                yield ev
        elif isinstance(chunk, ThinkingChunk):
            for ev in thinking.emit(chunk.delta):
                yield ev
        elif isinstance(chunk, (ToolUseChunk, ToolResultChunk)):
            thinking.close()
            async for ev in folder.emit_tool(chunk):
                yield ev
        elif isinstance(chunk, (InitChunk, PromptUsageChunk)) and self._on_note is not None:
            self._on_note(chunk)

    async def _get_event_iterator(self):
        if self._objs is None:
            return
        # The demux tee is active only when the sub-agent side-channel is wired
        # (a UI is bound); headless keeps the cheap filter-only path in
        # consume_cli_stream (Claude-side child traffic is simply dropped there).
        ledger = ActivityLedger(self._attach_activity)
        activity = ledger.recording(self._on_activity)
        folder = TextFolder(
            self._parts_manager,
            activity,
            activity_events=cli_activity_events,
            fold_text=lambda chunk, leading: fold_chunk_text(chunk, leading=leading),
            is_call=lambda chunk: isinstance(chunk, ToolUseChunk),
        )
        objs = self._demuxed_objs(activity, folder) if self._on_subagent is not None else self._objs
        thinking = _ThinkingParts(self._parts_manager)
        done: DoneChunk | None = None
        # aclosing() so an abandoned/cancelled consumer finalizes the chunk
        # pipeline rather than leaving it to GC. It reaches only that one
        # generator: the demux wrapper below it is left to the loop's
        # async-generator finalization, which is fine because it owns nothing
        # of its own — the turn iterator it reads from is closed explicitly by
        # request_stream's finally, which also interrupts the turn.
        async with aclosing(consume_cli_stream(objs)) as stream:
            async for chunk in stream:
                if isinstance(chunk, DoneChunk):
                    done = chunk
                    continue
                async for ev in self._events_for(chunk, folder, thinking):
                    ledger.note_event(ev)
                    yield ev
        self._finalize_done(done)
        # After the settle: a cancellation landing in this await must not
        # cost the turn its usage (same ordering as CodexStreamedResponse).
        if self._after is not None:
            await self._after()

    def _attach_activity(self, entries: list[dict]) -> None:
        """The ledger's first tool entry: expose it on the response (merged
        into any provider_details already set) so it survives into
        ``get()`` — including an interrupted stream's partial ``get()``."""
        self.provider_details = {**(self.provider_details or {}), CLI_ACTIVITY_KEY: entries}

    @property
    def model_name(self) -> str:
        return self._model_id

    @property
    def timestamp(self) -> datetime:
        return self._ts or datetime.now(tz=timezone.utc)

    @property
    def provider_name(self) -> str:
        return "claude-cli"

    @property
    def provider_url(self) -> str:
        return "https://claude.com/claude-code"
