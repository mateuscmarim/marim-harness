"""Pure helpers behind ``Harness.run_turn``'s per-turn context injection.

Two side-effect-free concerns live here, kept out of ``agent.py`` so the turn
loop module stays focused on orchestration:

* the ``<turn-context>`` envelope that wraps anything prepended to a turn's
  prompt (task checklist, finished-job digest, error note, hook output), so a
  resumed session can recover just the text the user typed; and
* ``actionable_error_note`` — the terse, sanitized note about a failed turn that
  is handed back to the *model* only when adjusting the next turn could help.

These are re-exported from ``agent.py`` for the public import surface and the
existing call sites/tests that import them from there."""

from __future__ import annotations

from ..tasks import Task, render_tasks
from .errors import provider_error_status

# Envelope wrapped around any context injected into a turn's prompt — job
# digests, error notes, and SessionStart/UserPromptSubmit hook output. It is
# prepended to what the user typed, so the typed text stays the suffix. The
# envelope gives that boundary a stable marker so a resumed session can show
# only what the user typed (matching the live TUI, which mounts the typed text
# before injection happens). Plain turns carry no envelope and are unchanged.
_TURN_CONTEXT_OPEN = "<turn-context>"
_TURN_CONTEXT_CLOSE = "</turn-context>"
_TURN_CONTEXT_SEP = f"{_TURN_CONTEXT_CLOSE}\n\n"


def wrap_turn_context(injected: str, typed: str) -> str:
    """Wrap ``injected`` context in the turn-context envelope and append the
    user's ``typed`` prompt after it. Inverse of :func:`strip_turn_context`."""
    return f"{_TURN_CONTEXT_OPEN}\n{injected}\n{_TURN_CONTEXT_SEP}{typed}"


def strip_turn_context(content: str) -> str:
    """Return only the user-typed portion of a persisted prompt, dropping any
    leading turn-context envelope that :meth:`Harness.run_turn` prepended. A
    prompt with no envelope is returned unchanged."""
    if not content.startswith(_TURN_CONTEXT_OPEN):
        return content
    # Anchor on the LAST separator, not the first. The forward contract
    # (`wrap_turn_context`) makes the user-typed text the suffix after the final
    # `</turn-context>\n\n` (note `_assemble_prompt` asserts the prompt ends with
    # `typed`). Injected context can legitimately contain the marker itself —
    # e.g. a SessionStart hook that echoes a prior turn's persisted prompt — so a
    # `find` (first occurrence) would stop inside the envelope and leak part of
    # it back as "typed". `rfind` recovers the true suffix regardless.
    idx = content.rfind(_TURN_CONTEXT_SEP)
    if idx == -1:
        return content
    return content[idx + len(_TURN_CONTEXT_SEP):]


def render_checklist_block(items: list[Task]) -> str:
    """The task-checklist block prepended to a turn's prompt as turn-state, or
    ``""`` when there are no tasks. It lives in the per-turn envelope rather than
    the system prompt so the cached system/tool prefix stays stable across
    turns."""
    if not items:
        return ""
    return (
        "Task checklist (✔ done · ▸ active · ○ pending):\n\n"
        + render_tasks(items)
    )


def _short(exc: BaseException, limit: int = 200) -> str:
    """A whitespace-collapsed, length-capped rendering of an exception — never a
    traceback, just the one-line gist that's safe to hand back to the model."""
    text = " ".join(str(exc).split())
    return text[: limit - 1] + "…" if len(text) > limit else text


def actionable_error_note(exc: BaseException) -> str | None:
    """A terse, sanitized note about a failed turn that the *model* can act on,
    or None when the failure is not the model's to fix. We surface only the
    errors where adjusting the next turn could plausibly help — a malformed or
    oversized request, a usage limit, the model failing to produce a usable
    response — and stay silent on harness/render bugs, cancellations, and
    transient infra (rate limits, 5xx), where a note would only mislead."""
    from pydantic_ai.exceptions import (
        ModelHTTPError,
        UnexpectedModelBehavior,
        UsageLimitExceeded,
    )

    head = "Note: your previous turn did not complete."
    if isinstance(exc, ModelHTTPError):
        # Client errors (context too long, malformed request) are the model's to
        # fix; rate limits (429) and server errors (5xx) are transient infra that
        # retrying — not re-prompting — should handle.
        if 400 <= exc.status_code < 500 and exc.status_code != 429:
            return (
                f"{head} The request was rejected (HTTP {exc.status_code}). "
                "Adjust your approach — e.g. shorten the input or fix the "
                "request — before continuing."
            )
        return None
    # A raw provider error (openai.APIError) that pydantic-ai didn't wrap as a
    # ModelHTTPError — common with OpenRouter's "Provider returned error". Apply
    # the same client-vs-transient split using the status the SDK or the body
    # carries; a 5xx/unknown is infra and gets no (misleading) note.
    provider_status = provider_error_status(exc)
    if provider_status is not None:
        if 400 <= provider_status < 500 and provider_status != 429:
            return (
                f"{head} The provider rejected the request "
                f"(HTTP {provider_status}: {_short(exc)}). Adjust your approach "
                "— e.g. shorten the input or fix the request — before continuing."
            )
        return None
    if isinstance(exc, UsageLimitExceeded):
        return (
            f"{head} A usage limit was reached ({_short(exc)}). Be more "
            "economical with tool calls and continue."
        )
    if isinstance(exc, UnexpectedModelBehavior):
        return f"{head} {_short(exc)}. Adjust your approach and continue."
    return None
