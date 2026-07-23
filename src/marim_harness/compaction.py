"""Context compaction: keep a conversation under a token budget.

When a history grows past the budget we keep the head (the original task anchor)
and a recent tail, and condense the middle. The tail must always begin at a clean
user-turn boundary so we never orphan a tool return from its tool call, which the
chat APIs reject.

Two strategies share the same head/tail split:

- ``compact_history`` (Phase 1): drop the middle entirely.
- ``compact_history_with_summary`` (Phase 2): summarize the middle into one
  synthetic message, falling back to truncation if the summary call fails.
"""

import dataclasses
import logging
import os
from collections.abc import Awaitable, Callable
from typing import Any

from pydantic_ai import Agent
from pydantic_ai.messages import (
    BinaryContent,
    ModelMessage,
    ModelRequest,
    TextPart,
    ThinkingPart,
    ToolCallPart,
    ToolReturnPart,
    UserPromptPart,
)

logger = logging.getLogger(__name__)

_CHARS_PER_TOKEN = 4
# Rough flat cost for a binary attachment (image). A vision model tokenizes an
# image by its pixels/tiles, NOT by its base64 size, so counting the raw bytes
# as text would wildly overcount (a ~500KB screenshot is ~1-2k image tokens, not
# ~500k). This nominal value keeps the context gauge and compaction planning sane.
_IMAGE_TOKEN_ESTIMATE = 1500

# (messages, custom_instructions) -> summary text. Instructions come from a
# manual `/compact <instructions>` and are None for automatic compaction.
Summarizer = Callable[[list[ModelMessage], str | None], Awaitable[str]]

# Sentinel so the compaction entry points can accept an already-computed tail
# start (None is a valid value — "nothing to drop") and reuse it instead of
# recomputing _plan_tail_start (which re-runs the whole-history estimate_tokens).
# Typed ``Any`` so the public params can stay ``int | None`` (the real domain)
# while still defaulting to this sentinel without widening ``start`` to ``object``.
_UNSET: Any = object()


def estimate_tokens(history: list[ModelMessage]) -> int:
    """Rough token estimate (~4 chars/token) over the serialized part content.

    Binary attachments (images) are counted as a flat nominal cost rather than by
    their base64 length, which would massively overcount — image bytes are not
    tokenized as text by vision models."""
    chars = 0
    images = 0
    for message in history:
        for part in getattr(message, "parts", []):
            content = getattr(part, "content", None)
            if isinstance(content, (list, tuple)):
                for item in content:
                    if isinstance(item, BinaryContent):
                        images += 1
                    elif item is not None:
                        chars += len(str(item))
            elif isinstance(content, BinaryContent):
                images += 1
            elif content is not None:
                chars += len(str(content))
            args = getattr(part, "args", None)
            if args is not None:
                chars += len(str(args))
    return chars // _CHARS_PER_TOKEN + images * _IMAGE_TOKEN_ESTIMATE


def last_request_input_tokens(history: list[ModelMessage]) -> int | None:
    """The provider-reported input-token count of the LAST model request in a run —
    the true size of the prompt as the provider tokenized it, i.e. the real current
    context size. The compaction gate uses this as a measured floor over its chars/4
    estimate, which undershoots dense code/JSON ~25% (see SessionController.maybe_compact
    and ``_measured_or_estimated``). NOT the run's cumulative ``result.usage``
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
        if (
            self.turns_since_compact is not None
            and self.turns_since_compact <= self.rapid_turns
        ):
            self.consecutive_rapid_refills += 1
        else:
            self.consecutive_rapid_refills = 0
        self.turns_since_compact = 0

    def reset(self) -> None:
        self.turns_since_compact = None
        self.consecutive_rapid_refills = 0


def _is_user_turn(message) -> bool:
    """True for a ModelRequest that opens a user turn (carries a UserPromptPart).

    Such a message is a safe tail boundary: it never holds a dangling tool return.
    """
    return isinstance(message, ModelRequest) and any(
        isinstance(part, UserPromptPart) for part in message.parts
    )


def _measured_or_estimated(history: list, measured_tokens: int | None) -> int:
    """The context size to gate compaction on: the larger of the char/4 estimate
    and the provider-reported ``measured_tokens`` (the ACTUAL input-token count of
    the last request), when one is supplied.

    The estimate divides raw chars by a flat 4, but dense code/JSON tokenizes at
    ~3 chars/token, so it undershoots the real window by ~25% and lets a session
    sail past the true limit before compaction fires. When the caller has the
    provider's real last-request count on hand it is authoritative, so prefer it —
    but take ``max`` rather than replacing outright, because ``history`` may have
    grown (the newest assistant reply) since that request was measured, and the
    estimate captures that tail the measurement predates. With no measurement
    (fresh session, provider that omits usage) we fall back to the estimate alone —
    exactly the legacy behavior."""
    estimated = estimate_tokens(history)
    if measured_tokens is None:
        return estimated
    return max(estimated, measured_tokens)


def _plan_tail_start(
    history: list, max_tokens: int, keep_last_messages: int, *, force: bool = False,
    measured_tokens: int | None = None,
) -> int | None:
    """Index where the kept tail should begin, or None if no compaction is needed.

    The tail always starts at a user-turn boundary so tool returns stay paired.
    ``force`` skips the token-size gate (used after a provider context-overflow
    error, where the estimate is known to have undershot the real window) and
    compacts down to the tail regardless — but still returns None when there is
    nothing meaningful to drop. ``measured_tokens`` is the provider's real
    last-request input-token count when available; see ``_measured_or_estimated``."""
    if not force and _measured_or_estimated(history, measured_tokens) <= max_tokens:
        return None

    user_turns = [i for i, m in enumerate(history) if _is_user_turn(m) and i > 0]
    if not user_turns:
        return None

    ideal_start = len(history) - keep_last_messages
    candidates = [i for i in user_turns if i <= ideal_start]
    start = candidates[-1] if candidates else user_turns[0]
    if start <= 1:
        # The tail would begin at index 1, i.e. nothing meaningful to drop.
        return None
    return start


def will_compact(
    history: list,
    max_tokens: int,
    keep_last_messages: int = 20,
    *,
    measured_tokens: int | None = None,
) -> bool:
    """Whether compacting ``history`` would actually drop anything — the same
    decision ``compact_history``/``compact_history_with_summary`` make, exposed
    so a caller can act *before* the (possibly expensive) compaction runs, e.g.
    firing a pre-compaction hook while the transcript is still full.

    Pass ``measured_tokens`` (the provider's real last-request input-token
    count) when it is known — ``maybe_compact`` gates on it, so a caller that
    omits it can reach the opposite verdict on a history the estimate
    undershoots."""
    return (
        _plan_tail_start(
            history, max_tokens, keep_last_messages, measured_tokens=measured_tokens
        )
        is not None
    )


def compact_history(
    history: list,
    max_tokens: int,
    keep_last_messages: int = 20,
    *,
    force: bool = False,
    tail_start: int | None = _UNSET,
) -> tuple[list, bool]:
    """Return (history, did_compact) by dropping the middle when over budget.

    ``tail_start`` lets a caller that already ran ``_plan_tail_start`` (e.g.
    ``SessionController.maybe_compact``, which also needs the decision to gate
    its PreCompact hook) pass it in so the whole-history token estimate isn't
    recomputed here. Left unset, it's computed as before."""
    start = (
        _plan_tail_start(history, max_tokens, keep_last_messages, force=force)
        if tail_start is _UNSET
        else tail_start
    )
    if start is None:
        return history, False
    compacted = history[:1] + history[start:]
    # Post-compaction sanity check: the tail planner can only cut on user-turn
    # boundaries, so when the overflow lives inside a single enormous turn the
    # "compacted" head+tail can still exceed the budget. This helper can't fix that
    # (it has no summarizer or masking lever), but the caller can (SessionController
    # masks stale observations on the forced-overflow path). Surface it so a still-
    # over-budget result isn't mistaken for a clean shrink.
    if estimate_tokens(compacted) > max_tokens:
        logger.debug(
            "compaction left history at ~%d tokens, still over the %d budget "
            "(likely one oversized turn the tail planner can't split)",
            estimate_tokens(compacted), max_tokens,
        )
    return compacted, True


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
    body = content[len(ELIDED_POINTER_PREFIX):]
    path, sep, _ = body.partition(_ELIDED_POINTER_SUFFIX)
    return path if sep else None


def _revalidate_parts(parts, exists: Callable[[str], bool]) -> tuple[list | None, int]:
    """Rewrite dangling pointer returns within one message's parts.

    Returns ``(new_parts, rewritten)`` — ``new_parts`` is None when nothing
    dangled, so the caller can skip rebuilding the message."""
    new_parts: list | None = None
    rewritten = 0
    for pidx, part in enumerate(parts):
        if not isinstance(part, ToolReturnPart):
            continue
        path = elided_pointer_path(part.content)
        if path is None or exists(path):
            continue
        if new_parts is None:
            new_parts = list(parts)
        new_parts[pidx] = dataclasses.replace(part, content=MASKED_OBSERVATION)
        rewritten += 1
    return new_parts, rewritten


def revalidate_elided_pointers(
    history: list, exists: Callable[[str], bool] = os.path.exists
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

    Same contract as :func:`mask_stale_observations`: never mutates the input
    (changed messages are rebuilt via ``replace``), idempotent (the plain
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
        new_parts, rewritten = _revalidate_parts(parts, exists)
        if new_parts is None:
            continue
        if new_history is None:
            new_history = list(history)
        new_history[idx] = dataclasses.replace(message, parts=new_parts)
        total += rewritten
    return (history, 0) if new_history is None else (new_history, total)


def _count_recent_parts(history: list, keep_recent: int) -> set[tuple[int, int]]:
    """Identify which ToolReturnParts should be kept (first pass: newest-first counting)."""
    seen = 0
    parts_to_keep: set[tuple[int, int]] = set()
    for idx in range(len(history) - 1, -1, -1):
        message = history[idx]
        parts = getattr(message, "parts", None)
        if not parts:
            continue
        for pidx in range(len(parts) - 1, -1, -1):
            part = parts[pidx]
            if not isinstance(part, ToolReturnPart):
                continue
            seen += 1
            if seen <= keep_recent:
                parts_to_keep.add((idx, pidx))
    return parts_to_keep


def _mask_part(
    part: ToolReturnPart,
    min_chars: int,
    persist: Callable[[str, str], str | None] | None,
) -> str | None:
    """Determine the replacement content for a part, or None if it should not be masked."""
    if _is_masked(part.content):
        return None
    content = part.content
    if isinstance(content, BinaryContent) or (
        isinstance(content, list) and any(isinstance(c, BinaryContent) for c in content)
    ):
        # An image observation is always large in effective tokens and has no
        # faithful text rendering to persist — mask it outright with the plain
        # placeholder. The original file is still on disk; a fresh read_file
        # brings it back if the model needs it again.
        return MASKED_OBSERVATION
    # Measure and persist the *model-facing* rendering, not the Python repr: for a
    # structured return str() yields `{'a': 1}` while the model actually read compact
    # JSON (`{"a":1}`). model_response_str() is pydantic-ai's exact wire rendering, so
    # the size gate charges what the context actually pays and the scratchpad copy is
    # byte-for-byte what the model saw — the pointer placeholder's read_file round-trip
    # stays faithful.
    try:
        rendered: str | None = part.model_response_str()
    except Exception:
        # Exotic content (e.g. multimodal/binary objects) pydantic can't serialize.
        # Same best-effort contract as persist: never block masking. There are no
        # faithful bytes to save, so fall back to the repr for the size gate and to
        # the plain placeholder (no pointer) below.
        rendered = None
    size = len(rendered) if rendered is not None else len(str(part.content))
    if size < min_chars:
        return None
    replacement = MASKED_OBSERVATION
    if persist is not None and rendered is not None:
        try:
            path = persist(rendered, part.tool_name)
        except Exception:
            path = None
        if path:
            replacement = _elided_pointer(path)
    return replacement


def mask_stale_observations(
    history: list,
    keep_recent: int = 4,
    *,
    min_chars: int = 200,
    persist: Callable[[str, str], str | None] | None = None,
) -> tuple[list, int]:
    """Replace the body of older tool-observation returns with a short placeholder.

    Walks ``history`` newest-first, leaves the most recent ``keep_recent``
    ``ToolReturnPart`` payloads intact (the agent is most likely still acting on
    them), and swaps the ``content`` of older returns whose rendered length is at
    least ``min_chars`` for :data:`MASKED_OBSERVATION`. A return's ``tool_name``
    and ``tool_call_id`` are preserved, so the tool-call/return pairing every chat
    API enforces is never broken — only the bulky payload is dropped.

    This is the cache-safe lever: it is meant to run *at compaction time*, when the
    cached message tail is already invalidated by the rewrite, so masking adds no
    extra cache miss (a per-request sliding mask would bust the tail cache every
    turn and cost more than it saves). Already-masked returns and small ones are
    skipped, so re-running it is idempotent. Returns ``(new_history, masked_count)``
    and never mutates the input — masked messages are rebuilt via ``replace``.

    When ``persist`` is given, it is called with ``(content, tool_name)`` and should
    write the payload somewhere recoverable, returning the path (used in a pointer
    placeholder) or ``None`` (falls back to plain placeholder). The payload handed to
    ``persist`` is the part's ``model_response_str()`` — byte-for-byte what the model
    originally read (compact JSON for structured returns, the string itself for str
    content) — and ``min_chars`` is measured on that same rendering. Persist is
    best-effort: a ``None``/failure never blocks masking, and content that cannot be
    rendered at all is masked with the plain placeholder instead of a pointer.
    """
    # First pass: identify which parts should be kept (newest-first counting).
    parts_to_keep = _count_recent_parts(history, keep_recent)

    # Second pass: apply masking in forward order so persist calls happen in document
    # order (oldest to newest).
    masked = 0
    new_history = list(history)
    for idx in range(len(history)):
        message = new_history[idx]
        parts = getattr(message, "parts", None)
        if not parts:
            continue
        new_parts = list(parts)
        changed = False
        for pidx in range(len(parts)):
            part = parts[pidx]
            if not isinstance(part, ToolReturnPart):
                continue
            # Skip if this part should be kept.
            if (idx, pidx) in parts_to_keep:
                continue
            # Determine replacement content (or None to skip).
            replacement = _mask_part(part, min_chars, persist)
            if replacement is None:
                continue
            new_parts[pidx] = dataclasses.replace(part, content=replacement)
            changed = True
            masked += 1
        if changed:
            new_history[idx] = dataclasses.replace(message, parts=new_parts)
    return new_history, masked


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
                    lines.append(
                        f"Assistant (thinking): {_clip(part.content, max_part_chars)}"
                    )
            elif isinstance(part, ToolCallPart):
                lines.append(
                    f"Assistant called {part.tool_name}"
                    f"({_clip(part.args, max_part_chars)})"
                )
            elif isinstance(part, ToolReturnPart):
                lines.append(_render_tool_return(part, max_part_chars))
    return "\n".join(lines)


def _clip(value, limit: int) -> str:
    text = str(value)
    return text if len(text) <= limit else text[:limit] + "…"


def _render_tool_return(part: ToolReturnPart, max_part_chars: int) -> str:
    """Render a ToolReturnPart as a transcript line, with BinaryContent as a placeholder."""
    content: object = part.content
    if isinstance(content, BinaryContent):
        content = f"[image {content.media_type}]"
    return f"Tool {part.tool_name} returned: {_clip(content, max_part_chars)}"


# Marks the synthetic message that replaces a compacted middle. The TUI keys off
# this prefix to render the summary as a distinct block instead of a user message.
SUMMARY_PREFIX = "[Summary of earlier conversation, condensed to save context]"


def summary_text(content) -> str | None:
    """Return the summary body if ``content`` is a compaction summary message
    (a ``str`` starting with :data:`SUMMARY_PREFIX` followed by a non-empty body),
    else ``None``. The single source of truth for detecting/parsing a summary."""
    if not isinstance(content, str) or not content.startswith(SUMMARY_PREFIX):
        return None
    body = content[len(SUMMARY_PREFIX):].strip()
    return body or None


def _summary_message(summary: str) -> ModelRequest:
    return ModelRequest(
        parts=[UserPromptPart(content=f"{SUMMARY_PREFIX}\n\n{summary}")]
    )


async def compact_history_with_summary(
    history: list,
    max_tokens: int,
    summarizer: Summarizer,
    keep_last_messages: int = 20,
    *,
    force: bool = False,
    tail_start: int | None = _UNSET,
    instructions: str | None = None,
) -> tuple[list, bool]:
    """Like ``compact_history`` but replace the dropped middle with a summary.

    Calls ``summarizer`` with the middle messages. If it raises or returns an
    empty string, falls back to plain truncation so a flaky summary model can
    never break a turn.

    ``tail_start`` behaves as in ``compact_history``: pass a precomputed
    ``_plan_tail_start`` result to avoid recomputing the whole-history estimate.
    ``instructions`` is the manual `/compact <instructions>` focus, threaded
    through to the summarizer verbatim; ``None`` for automatic compaction.
    """
    start = (
        _plan_tail_start(history, max_tokens, keep_last_messages, force=force)
        if tail_start is _UNSET
        else tail_start
    )
    if start is None:
        return history, False

    middle = history[1:start]
    summary: str | None
    try:
        summary = await summarizer(middle, instructions)
    except Exception as exc:
        logger.warning("compaction summarizer failed, falling back to truncation: %s", exc)
        summary = None

    if summary:
        return history[:1] + [_summary_message(summary)] + history[start:], True
    return history[:1] + history[start:], True


Titler = Callable[[list[ModelMessage]], Awaitable[str]]

_SUMMARY_INSTRUCTIONS = (
    "You compress a coding-session transcript into a dense summary so the agent "
    "can keep working with less context. Write terse notes, not prose, under "
    "these headings:\n"
    "1. Primary request and intent — every explicit ask from the user.\n"
    "2. Key technical concepts — technologies, patterns, decisions.\n"
    "3. Files and code sections — files read or edited, what changed and why, "
    "with short snippets only where essential to continue.\n"
    "4. Errors and fixes — each error hit, how it was fixed, and any user "
    "feedback about doing it differently.\n"
    "5. All user messages — every non-tool-result user message, condensed but "
    "none omitted.\n"
    "6. Pending tasks — work explicitly requested but not finished.\n"
    "7. Current work — precisely what was in progress at the cut, file names "
    "and snippets included.\n"
    "8. Next step — only if directly in line with the most recent explicit "
    "request; include a verbatim quote from the recent conversation showing "
    "where work left off, so the task cannot drift.\n"
    "Security-relevant user instructions (files or data to avoid, operations "
    "that must not be performed, credential handling rules) MUST be preserved "
    "verbatim so they continue to apply after compaction. Drop pleasantries "
    "and redundant detail."
)

_TITLE_INSTRUCTIONS = (
    "You write a short, specific title for a coding session from its transcript. "
    "Reply with the title only — no quotes, no trailing punctuation, at most six "
    "words. Name the concrete task, e.g. 'Fix the parser off-by-one' or 'Add "
    "session auto-naming'."
)

_MAX_TITLE_CHARS = 50


def _summarize_prompt(transcript: str, instructions: str | None = None) -> str:
    """Wrap the transcript in an explicit, in-message summarize instruction. A bare
    transcript with the rules only in the system prompt lets weaker models reply
    conversationally instead of summarizing; restating the task in the user turn
    and delimiting the transcript keeps them on task. ``instructions`` is the
    user's manual `/compact` focus, honored as an extra block the summarizer is
    told to follow."""
    extra = (
        f"\n\n## Compact instructions\nAlso follow these user-supplied "
        f"instructions when summarizing:\n{instructions}\n"
        if instructions
        else ""
    )
    return (
        "Summarize the coding-session transcript below into dense notes under "
        "the headings from your instructions. Output only the summary — do not "
        "reply conversationally or address the user."
        f"{extra}\n\n"
        "=== TRANSCRIPT START ===\n"
        f"{transcript}\n"
        "=== TRANSCRIPT END ===\n\n"
        "Summary:"
    )


def make_summarizer(model) -> Summarizer:
    """Build a summarizer backed by a dedicated, tool-free agent on ``model``."""
    summary_agent = Agent(model, instructions=_SUMMARY_INSTRUCTIONS)

    async def summarize(messages: list, instructions: str | None = None) -> str:
        result = await summary_agent.run(
            _summarize_prompt(render_transcript(messages), instructions)
        )
        return result.output

    return summarize


def clean_title(raw: str) -> str:
    """Reduce a model's reply to a single tidy title line, with a safe fallback."""
    lines = [line.strip() for line in (raw or "").splitlines()]
    text = next((line for line in lines if line), "")
    if text.lower().startswith("title:"):
        text = text[len("title:"):].strip()
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
