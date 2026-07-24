"""Headless (non-interactive) execution: run one turn and render the result to
a stream, without the TUI. Supports three output formats — plain text, a single
JSON object, and newline-delimited JSON streaming."""

import json
import logging
import sys
from collections.abc import Awaitable, Callable
from typing import TypeVar

from ...runtime.errors import format_provider_error
from ...runtime.harness import Harness
from ...stream_events import event_to_dict
from ...usage import usage_summary

logger = logging.getLogger(__name__)

_T = TypeVar("_T")


def _safe(fn: Callable[[], _T]) -> None:
    """Run a teardown step, swallowing and logging any error. Cleanup must never
    raise out of the ``finally`` block — that would mask the real turn error and
    the exit code the caller relies on."""
    try:
        fn()
    except Exception as exc:  # noqa: BLE001 - cleanup is best-effort
        logger.warning("Ignoring error during headless cleanup: %s", exc, exc_info=True)


async def _safe_async(fn: Callable[[], Awaitable[_T]]) -> None:
    """Async counterpart to ``_safe`` for awaitable teardown steps."""
    try:
        await fn()
    except Exception as exc:  # noqa: BLE001 - cleanup is best-effort
        logger.warning("Ignoring error during headless cleanup: %s", exc, exc_info=True)


def _notify(harness: Harness, title: str, body: str, event_type: str) -> None:
    """Fire a desktop notification if one is wired on deps. Best-effort."""
    notifier = harness.deps.ui.notifier
    if notifier is not None:
        notifier.send(title, body, event_type)


def _preview(text: str, max_len: int = 80) -> str:
    """Return a short preview of *text* for notification bodies.

    Newlines are collapsed to spaces and the result is truncated to *max_len*
    characters, with an ellipsis when trimmed.  Empty input yields a
    generic fallback so the notification is never blank."""
    if not text or not text.strip():
        return "(empty response)"
    collapsed = " ".join(text.split())
    if len(collapsed) <= max_len:
        return collapsed
    return collapsed[: max_len - 1] + "…"


def _usage_dict(harness: Harness) -> dict:
    return usage_summary(harness.session.usage, harness.model_id)


def _result_obj(harness: Harness, output: str) -> dict:
    store = harness.session.store
    return {
        "type": "result",
        "output": output,
        "session_id": store.session_id if store is not None else None,
        "name": harness.session.session_name,
        "usage": _usage_dict(harness),
    }


async def run_headless(
    harness: Harness,
    prompt: str,
    output_format: str,
    *,
    out=sys.stdout,
    err=sys.stderr,
) -> int:
    """Run a single turn and render it in ``output_format`` (``text``, ``json``,
    or ``stream-json``). Returns a process exit code: 0 on success, 1 on a turn
    failure (the error is written to ``err``).

    Always runs the turn in streaming mode — for ``stream-json`` the events are
    emitted as NDJSON, otherwise they are drained silently. This mirrors the TUI,
    which streams every turn; some providers' non-streaming endpoints are flakier
    than their streaming ones, so streaming here keeps headless runs as reliable
    as the interactive app."""

    async def handler(ctx, events):
        async for event in events:
            if output_format != "stream-json":
                continue  # drain to force a streaming request; emit nothing
            obj = event_to_dict(event)
            if obj is not None:
                print(json.dumps(obj), file=out, flush=True)

    try:
        await harness.connect()  # open any configured MCP servers for this run
        await harness.session_start("resume" if harness.session.history else "startup")
        output = await harness.run_turn(prompt, event_stream_handler=handler)
    except Exception as exc:  # keep the failure surface small and scriptable
        detail = format_provider_error(exc) or f"{type(exc).__name__}: {exc}"
        print(detail, file=err)
        _notify(harness, "Turn error", detail, "error")
        logger.warning("headless turn failed: %s", exc, exc_info=True)
        # stream-json consumers parse NDJSON and need a terminal line even on
        # failure, otherwise a crashed turn looks like a truncated stream.
        if output_format == "stream-json":
            print(json.dumps({"type": "error", "error": detail}), file=out, flush=True)
        return 1
    finally:
        # The turn end only *schedules* the background autoname; this one-shot
        # process would exit before it lands, so settle it here — before the
        # final persist and the result rendering below (which reports the
        # session name). A no-op when nothing was scheduled (e.g. the error
        # path, or an already-named session).
        await _safe_async(harness.session.wait_autoname)
        # Fold this run's active time into the total and force a persist so the
        # final segment lands even when history is unchanged — the TUI does the
        # same in on_unmount before teardown. Then run lifecycle teardown.
        # Every step is guarded: a raising finalize/session_end/aclose must not
        # mask the real turn error (or its exit code) that brought us here.
        _safe(harness.session.finalize_active_time)
        _safe(lambda: harness.session.persist(force=True))
        await _safe_async(lambda: harness.session_end("exit"))
        await _safe_async(harness.aclose)

    _notify(harness, "Turn complete", _preview(output), "turn_complete")

    if output_format == "json":
        obj = _result_obj(harness, output)
        del obj["type"]  # the single-object form has no event envelope
        print(json.dumps(obj), file=out)
    elif output_format == "stream-json":
        print(json.dumps(_result_obj(harness, output)), file=out)
    else:  # text
        print(output, file=out)
    return 0
