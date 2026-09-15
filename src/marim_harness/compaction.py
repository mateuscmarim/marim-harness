"""Compatibility helpers for compacted histories, transcripts, and titles.

Pydantic AI Harness owns active compaction and token estimation. This module keeps
the small readers needed for sessions written by older marim releases, plus helpers
used by transcript rendering, session titles, and the rapid-refill breaker.
"""

import dataclasses
import os
from collections.abc import Awaitable, Callable
from pathlib import Path

from pydantic_ai import Agent
from pydantic_ai.messages import (
    ModelMessage,
    ModelRequest,
    TextPart,
    ThinkingPart,
    ToolCallPart,
    ToolReturnPart,
    UserPromptPart,
)
from pydantic_ai_harness.compaction import estimate_token_count

from .binary_safe import has_binary_content, render_binary_safe
from .tools.impl.offload import OFFLOAD_GONE_NOTE, find_offload_paths


def estimate_tokens(history: list[ModelMessage]) -> int:
    """Approximate message tokens through Harness's public estimator.

    The wrapper preserves marim's long-standing import for UI and server readers
    while keeping estimator behavior aligned with the active upstream strategies.
    """
    return estimate_token_count(history)


def _measured_or_estimated(history: list[ModelMessage], measured_tokens: int | None) -> int:
    """Use provider measurement as a floor over the public upstream estimate."""
    estimated = estimate_tokens(history)
    return estimated if measured_tokens is None else max(estimated, measured_tokens)


def last_request_input_tokens(history: list[ModelMessage]) -> int | None:
    """The provider-reported input-token count of the LAST model request in a run —
    the true size of the prompt as the provider tokenized it, i.e. the real current
    context size. The compaction gate uses this as a measured floor over its estimate
    (see ``SessionController.maybe_compact``). NOT the run's cumulative ``result.usage``
    input tokens — that sums every step of a multi-request turn and would overshoot the
    live context size. Returns ``None`` when no response carries usage (some
    providers/streams omit it), which leaves the gate on the estimate alone."""
    for message in reversed(history):
        usage = getattr(message, "usage", None)
        tokens = getattr(usage, "input_tokens", None)
        if tokens:
            return int(tokens)
    return None


# Shown once when the breaker opens; mirrors Claude Code's thrashing message.
BREAKER_NOTICE = (
    "Auto-compaction is thrashing: the context refilled right after each of the "
    "last 3 compactions. A file read or tool output is likely too large for the "
    "context window — read in smaller chunks, or /clear to start fresh."
)


@dataclasses.dataclass
class CompactionBreaker:
    """Rapid-refill circuit breaker for auto-compaction.

    If a compaction's result refills past the threshold within ``rapid_turns``
    turns, ``trip_after`` consecutive times, the breaker opens and the caller
    should skip *auto* compaction (manual and forced compaction bypass it).
    Without this, one oversized tool observation re-triggers the summarizer
    every turn forever — burning summarizer calls without ever getting under
    the threshold. Pure state machine: the owner calls ``note_turn()`` once per
    post-turn compaction check and ``note_compact()`` when a compaction fires.
    """

    rapid_turns: int = 3
    trip_after: int = 3
    turns_since_compact: int | None = None  # None until the first compaction
    consecutive_rapid_refills: int = 0

    @property
    def open(self) -> bool:
        return self.consecutive_rapid_refills >= self.trip_after

    def note_turn(self) -> None:
        if self.turns_since_compact is not None:
            self.turns_since_compact += 1

    def note_compact(self) -> None:
        if self.turns_since_compact is not None and self.turns_since_compact <= self.rapid_turns:
            self.consecutive_rapid_refills += 1
        else:
            self.consecutive_rapid_refills = 0
        self.turns_since_compact = 0

    def reset(self) -> None:
        self.turns_since_compact = None
        self.consecutive_rapid_refills = 0


# Replaces a stale tool observation's body. Kept short and explicit so the model
# knows the output was *elided*, not lost, and can re-run the tool if it still
# needs it — the same contract read_file/run_bash already use when they clip.
MASKED_OBSERVATION = (
    "[observation elided to save context — re-run the tool if you need this output]"
)

# When the payload was persisted to the session scratchpad before eliding, the
# placeholder points at the file so the model can recover the exact bytes with
# read_file instead of re-running the tool. Both placeholder forms are treated
# as already-masked by _is_masked, keeping re-runs idempotent.
ELIDED_POINTER_PREFIX = "[output elided to save context; full content at "


def _elided_pointer(path: str) -> str:
    return f"{ELIDED_POINTER_PREFIX}{path} — read_file it if still needed]"


def _is_masked(content) -> bool:
    return content == MASKED_OBSERVATION or (
        isinstance(content, str) and content.startswith(ELIDED_POINTER_PREFIX)
    )


# The suffix _elided_pointer appends after the path; also the parse anchor for
# elided_pointer_path. A path containing this exact string would truncate the
# parse, but persist_elided generates the paths (scratchpad + counter + slug),
# so the em-dash phrase can never legitimately appear inside one.
_ELIDED_POINTER_SUFFIX = " — read_file"


def elided_pointer_path(content) -> str | None:
    """The scratchpad path inside an elided-pointer placeholder, or None.

    Inverse of :func:`_elided_pointer`: parses the path between
    :data:`ELIDED_POINTER_PREFIX` and the `` — read_file`` suffix. Anything that
    isn't a well-formed pointer string (plain content, :data:`MASKED_OBSERVATION`,
    structured returns, a prefix with no suffix) yields None."""
    if not isinstance(content, str) or not content.startswith(ELIDED_POINTER_PREFIX):
        return None
    body = content[len(ELIDED_POINTER_PREFIX) :]
    path, sep, _ = body.partition(_ELIDED_POINTER_SUFFIX)
    return path if sep else None


def repair_masked_narrowed_returns(raw_messages) -> int:
    """Strip ``tool_kind`` from raw parts an older marim masked into invalidity.

    Runs on the *raw* JSON, before ``ModelMessagesTypeAdapter`` validation, and
    mutates ``raw_messages`` in place; returns how many parts it repaired.

    Before :func:`has_narrowed_content` existed, the masker would replace a typed
    tool-return's ``content`` (e.g. ``ToolSearchReturnPart``'s
    ``ToolSearchReturnContent`` TypedDict) with a placeholder string. Those
    sessions are already on disk, and they no longer validate at all — the
    discriminated union selects the typed member from ``tool_kind`` and then
    demands an object for ``content`` — so resuming one raises
    ``SessionLoadError`` instead of loading.

    Dropping ``tool_kind`` demotes the part to a plain ``ToolReturnPart``, which
    is what it honestly is now: its typed payload is gone and only the
    "re-run the tool" placeholder remains. The reveal state it used to carry is
    lost either way; under-counting there is safe by pydantic-ai's own contract
    (a redundant tool search is idempotent, whereas over-counting would claim a
    tool is visible when it is not).

    Deliberately narrow: only parts whose content is one of *our* placeholders
    are touched. Any other typed-content mismatch is a corruption marim did not
    cause, and must keep failing loudly rather than being silently reshaped.
    """
    repaired = 0
    if not isinstance(raw_messages, list):
        return 0
    for message in raw_messages:
        if not isinstance(message, dict):
            continue
        for part in message.get("parts") or []:
            if not isinstance(part, dict) or part.get("tool_kind") is None:
                continue
            if not _is_masked(part.get("content")):
                continue
            part.pop("tool_kind", None)
            repaired += 1
    return repaired


def _annotate_dangling_handles(
    content, exists: Callable[[str], bool], base: Path | None
) -> str | None:
    """*content* with :data:`OFFLOAD_GONE_NOTE` appended when any offload-handle
    path inside it no longer exists, or None when nothing needs annotating.

    Append, never replace: unlike an elided pointer (whose whole content IS the
    placeholder), a handle carries a real inline preview that must survive.
    Idempotent via the note itself — content already annotated is skipped, so
    one note per part even with several dangling paths (the note says
    "referenced above" rather than naming one). Non-absolute paths (legacy
    histories predating absolute spill paths) resolve against *base*."""
    if not isinstance(content, str) or OFFLOAD_GONE_NOTE in content:
        return None
    paths = find_offload_paths(content)
    if not paths:
        return None

    def resolved(p: str) -> str:
        return p if os.path.isabs(p) or base is None else str(base / p)

    if all(exists(resolved(p)) for p in paths):
        return None
    return content + OFFLOAD_GONE_NOTE


def _revalidate_parts(
    parts, exists: Callable[[str], bool], base: Path | None
) -> tuple[list | None, int]:
    """Rewrite dangling scratchpad references within one message's parts.

    Two detectors, mutually exclusive by construction (their copy differs on
    purpose): a dangling elided POINTER is replaced with the plain masked
    placeholder (nothing to preserve), a dangling offload HANDLE gets the
    gone-note appended (the preview survives). Returns ``(new_parts,
    rewritten)`` — ``new_parts`` is None when nothing dangled, so the caller
    can skip rebuilding the message."""
    new_parts: list | None = None
    rewritten = 0
    for pidx, part in enumerate(parts):
        if not isinstance(part, ToolReturnPart):
            continue
        replacement: object | None = None
        path = elided_pointer_path(part.content)
        if path is not None:
            if not exists(path):
                replacement = MASKED_OBSERVATION
        else:
            replacement = _annotate_dangling_handles(part.content, exists, base)
        if replacement is None:
            continue
        if new_parts is None:
            new_parts = list(parts)
        new_parts[pidx] = dataclasses.replace(part, content=replacement)
        rewritten += 1
    return new_parts, rewritten


def revalidate_elided_pointers(
    history: list,
    exists: Callable[[str], bool] = os.path.exists,
    base: Path | None = None,
) -> tuple[list, int]:
    """Degrade elided-pointer placeholders whose backing file no longer exists.

    A pointer placeholder promises the model it can ``read_file`` the elided
    payload back — but the scratchpad lives under /tmp, so a session can outlive
    it (reboot, systemd-tmpfiles aging). This scans ``history`` for
    ``ToolReturnPart``s whose content is a pointer, checks the pointed-at path
    via ``exists`` (injectable for tests; defaults to the real filesystem), and
    rewrites dangling ones to plain :data:`MASKED_OBSERVATION`, which honestly
    tells the model to re-run the tool instead. Live pointers and every other
    part are left untouched.

    Offload HANDLES (the ``saved to `path` `` envelope from
    tools/impl/offload.py) are revalidated in the same walk: a dangling one
    gets OFFLOAD_GONE_NOTE appended — preview preserved — rather than being
    replaced. ``base`` resolves non-absolute handle paths (legacy histories)
    against the workspace root; absolute paths ignore it.

    This compatibility reader never mutates the input (changed messages are rebuilt
    via ``replace``), is idempotent (the plain
    placeholder parses as no pointer), and returns ``(new_history, rewritten)``.
    One deliberate difference: when nothing dangles the SAME ``history`` object
    comes back, so callers can ``is``-check for change and skip a history
    replacement (and the version bump / persist it would trigger)."""
    new_history: list | None = None
    total = 0
    for idx, message in enumerate(history):
        parts = getattr(message, "parts", None)
        if not parts:
            continue
        new_parts, rewritten = _revalidate_parts(parts, exists, base)
        if new_parts is None:
            continue
        if new_history is None:
            new_history = list(history)
        new_history[idx] = dataclasses.replace(message, parts=new_parts)
        total += rewritten
    return (history, 0) if new_history is None else (new_history, total)


def render_transcript(messages: list, max_part_chars: int = 2000) -> str:
    """Flatten messages into a plain-text transcript for the summarizer.

    Rendering to text (rather than passing the slice as message history) sidesteps
    the tool-call/return pairing rules — the summarizer just reads prose.
    """
    lines: list[str] = []
    for message in messages:
        role = "User" if isinstance(message, ModelRequest) else "Assistant"
        for part in getattr(message, "parts", []):
            if isinstance(part, UserPromptPart):
                lines.append(f"{role}: {_clip(part.content, max_part_chars)}")
            elif isinstance(part, TextPart):
                if part.content:
                    lines.append(f"Assistant: {_clip(part.content, max_part_chars)}")
            elif isinstance(part, ThinkingPart):
                if part.content:
                    lines.append(f"Assistant (thinking): {_clip(part.content, max_part_chars)}")
            elif isinstance(part, ToolCallPart):
                lines.append(
                    f"Assistant called {part.tool_name}({_clip(part.args, max_part_chars)})"
                )
            elif isinstance(part, ToolReturnPart):
                lines.append(_render_tool_return(part, max_part_chars))
    return "\n".join(lines)


def _clip(value, limit: int) -> str:
    text = str(value)
    return text if len(text) <= limit else text[:limit] + "…"


def _render_tool_return(part: ToolReturnPart, max_part_chars: int) -> str:
    """Render a ToolReturnPart as a transcript line, with BinaryContent — scalar OR
    inside a list (an MCP tool can return mixed text/image content blocks) —
    rendered as the shared binary-safe placeholder. The advisor sees this
    transcript over the wire, so a raw BinaryContent repr (its base64 body) must
    never reach it; only the non-binary path clips through the normal ``_clip``
    text budget."""
    content: object = part.content
    if has_binary_content(content):
        return f"Tool {part.tool_name} returned: {render_binary_safe(content)}"
    return f"Tool {part.tool_name} returned: {_clip(content, max_part_chars)}"


# Marks the synthetic message that replaces a compacted middle. The TUI keys off
# this prefix to render the summary as a distinct block instead of a user message.
SUMMARY_PREFIX = "[Summary of earlier conversation, condensed to save context]"
UPSTREAM_SUMMARY_PREFIX = "Summary of previous conversation:\n\n"


def summary_text(content) -> str | None:
    """Return the summary body if ``content`` is a compaction summary message
    (a ``str`` starting with :data:`SUMMARY_PREFIX` followed by a non-empty body),
    else ``None``. The single source of truth for detecting/parsing a summary."""
    if not isinstance(content, str):
        return None
    prefix = next(
        (
            prefix
            for prefix in (SUMMARY_PREFIX, UPSTREAM_SUMMARY_PREFIX)
            if content.startswith(prefix)
        ),
        None,
    )
    if prefix is None:
        return None
    body = content[len(prefix) :].strip()
    return body or None


Titler = Callable[[list[ModelMessage]], Awaitable[str]]

_TITLE_INSTRUCTIONS = (
    "You write a short, specific title for a coding session from its transcript. "
    "Reply with the title only — no quotes, no trailing punctuation, at most six "
    "words. Name the concrete task, e.g. 'Fix the parser off-by-one' or 'Add "
    "session auto-naming'."
)

_MAX_TITLE_CHARS = 50


def clean_title(raw: str) -> str:
    """Reduce a model's reply to a single tidy title line, with a safe fallback."""
    lines = [line.strip() for line in (raw or "").splitlines()]
    text = next((line for line in lines if line), "")
    if text.lower().startswith("title:"):
        text = text[len("title:") :].strip()
    text = text.strip("\"'`").strip().rstrip(".!?,;:").strip()
    if len(text) > _MAX_TITLE_CHARS:
        text = text[:_MAX_TITLE_CHARS].rstrip() + "…"
    return text or "Untitled session"


def _title_prompt(transcript: str) -> str:
    """Wrap the transcript in an explicit, in-message title instruction. As with
    ``_summarize_prompt``, a bare transcript with the rules only in the system
    prompt lets a model reply conversationally instead of titling — and under the
    claude-cli provider our instruction is merely *appended* to Claude Code's own
    system prompt, so restating the task in the user turn is what keeps it on
    task. Without this, the model's chat reply becomes the session name."""
    return (
        "Write a short, specific title (at most six words) for the coding-session "
        "transcript below, following the rules in your instructions. Output only "
        "the title — no quotes, no trailing punctuation, do not reply "
        "conversationally or address the user.\n\n"
        "=== TRANSCRIPT START ===\n"
        f"{transcript}\n"
        "=== TRANSCRIPT END ===\n\n"
        "Title:"
    )


def make_titler(model) -> Titler:
    """Build a titler backed by a dedicated, tool-free agent on ``model``."""
    title_agent = Agent(model, instructions=_TITLE_INSTRUCTIONS)

    async def title(messages: list) -> str:
        result = await title_agent.run(_title_prompt(render_transcript(messages)))
        return clean_title(result.output)

    return title
