"""Pure helpers behind ``Harness.run_turn``'s per-turn context injection.

Two side-effect-free concerns live here, kept out of ``controller.py`` so the
turn loop module stays focused on orchestration:

* the ``<turn-context>`` envelope that wraps anything prepended to a turn's
  prompt (task checklist, finished-job digest, error note, hook output), so a
  resumed session can recover just the text the user typed; and
* ``actionable_error_note`` — the terse, sanitized note about a failed turn that
  is handed back to the *model* only when adjusting the next turn could help.

These are re-exported from ``harness.py`` for the public import surface and the
existing call sites/tests that import them from there."""

from __future__ import annotations

import re
from collections.abc import Sequence
from datetime import date

from ..tasks import Task, render_tasks
from .errors import provider_error_status

# Envelope wrapped around any context injected into a turn's prompt — job
# digests, error notes, and SessionStart/UserPromptSubmit hook output. It is
# prepended to what the user typed, so the typed text stays the suffix. The
# envelope gives that boundary a stable marker so a resumed session can show
# only what the user typed (matching the live TUI, which mounts the typed text
# before injection happens). Plain turns carry no envelope and are unchanged.
#
# The opening tag carries the exact character length of the user-typed suffix
# (`<turn-context len="N">`). Recovery then slices the last N characters —
# unambiguous no matter what either side contains. The older marker-only format
# (`<turn-context>`, no length) recovered the suffix by searching for the
# closing separator, which is ambiguous from BOTH ends: injected context may
# echo the marker (so a first-match search leaks part of the envelope), and the
# user's OWN typed text may contain `</turn-context>\n\n` (so a last-match
# search truncates the typed text to whatever follows their marker). The length
# prefix removes the guess entirely. `strip_turn_context` still understands the
# old format for sessions persisted before this change.
_TURN_CONTEXT_CLOSE = "</turn-context>"
_TURN_CONTEXT_SEP = f"{_TURN_CONTEXT_CLOSE}\n\n"
# v1 (legacy) open marker, kept only for reading old persisted sessions.
_TURN_CONTEXT_OPEN_V1 = "<turn-context>"
# v2 open tag with the typed-suffix length; the group captures N.
_TURN_CONTEXT_OPEN_V2_RE = re.compile(r'^<turn-context len="(\d+)">')


def wrap_turn_context(injected: str, typed: str) -> str:
    """Wrap ``injected`` context in the turn-context envelope and append the
    user's ``typed`` prompt after it. Inverse of :func:`strip_turn_context`.

    The opening tag records ``len(typed)`` so the recovery can slice the typed
    suffix by length rather than by searching for the separator — robust even
    when ``typed`` (or ``injected``) itself contains the ``</turn-context>``
    marker."""
    return (
        f'<turn-context len="{len(typed)}">\n{injected}\n{_TURN_CONTEXT_SEP}{typed}'
    )


def strip_turn_context(content: str) -> str:
    """Return only the user-typed portion of a persisted prompt, dropping any
    leading turn-context envelope that :meth:`Harness.run_turn` prepended. A
    prompt with no envelope is returned unchanged."""
    m = _TURN_CONTEXT_OPEN_V2_RE.match(content)
    if m is not None:
        # v2: the tag states the typed length exactly. Slice the last N chars —
        # `content[-0:]` would wrongly return the whole string, so an empty
        # typed suffix (N == 0, e.g. a background-digest-only turn) short-circuits.
        n = int(m.group(1))
        return content[len(content) - n:] if n else ""
    if not content.startswith(_TURN_CONTEXT_OPEN_V1):
        return content
    # v1 (legacy) fallback: no length was recorded, so anchor on the LAST
    # separator. The forward contract made the typed text the suffix after the
    # final `</turn-context>\n\n`; injected context could echo the marker, so a
    # first-match search would stop inside the envelope. `rfind` recovers the
    # suffix in the common case (it is still fooled by a marker in the typed
    # text — exactly the ambiguity the v2 length prefix fixes going forward).
    idx = content.rfind(_TURN_CONTEXT_SEP)
    if idx == -1:
        return content
    return content[idx + len(_TURN_CONTEXT_SEP):]


_PLAN_MODE_PREAMBLE = (
    "You are in PLAN MODE. Research the task read-only — read files, search, and "
    "use read-only shell commands (git status/log/diff, ls, grep). Do NOT write, "
    "edit, or run mutating commands; they will be denied. When you have a concrete "
    "plan, call `present_plan` with a one-paragraph summary and the ordered steps. "
    "Do not start implementing until the user approves."
)


def plan_mode_preamble() -> str:
    """The planning instruction injected into a turn's context when the session is
    in plan mode. Lives in the per-turn envelope (not the system prompt) so the
    cached system/tool prefix stays stable across turns."""
    return _PLAN_MODE_PREAMBLE


def render_current_date_block() -> str:
    """Return the current date as a short block for the turn-context envelope.

    Placed in the per-turn user message (not the system prompt) so the cached
    system/tool prefix stays stable across turns and across day boundaries."""
    return f"Current date: {date.today().isoformat()}"


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


def render_shell_results_block(
    results: Sequence[tuple[str, str]], dropped: int = 0
) -> str:
    """The ``<user-shell-commands>`` block for the turn-context envelope, or
    ``""`` when there is nothing to show (falsy-when-empty, matching
    :func:`render_checklist_block` so callers can ``if block:``).

    Each entry is ``(command, output)`` from the TUI's ``!`` passthrough —
    commands the user ran themselves, whose output is already on their screen.
    The block exists so the model can see what the user saw. ``dropped`` counts
    entries the controller's budget cap elided; it is surfaced as a marker line
    so the model knows the list is incomplete rather than assuming it saw
    everything."""
    if not results:
        return ""
    lines = [
        "<user-shell-commands>",
        "The user ran these commands directly in their own shell (via the `!` "
        "prompt passthrough). The outputs are shown verbatim and are already "
        "visible to the user.",
    ]
    if dropped:
        lines.append(f"({dropped} earlier command(s) elided to fit the context budget)")
    for command, output in results:
        lines.append(f"$ {command}")
        lines.append(output)
    lines.append("</user-shell-commands>")
    return "\n".join(lines)


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
