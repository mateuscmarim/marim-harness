"""Surface the *real* provider error behind a failed turn.

OpenRouter (via the OpenAI SDK) raises ``openai.APIError`` with a terse message
like ``"Provider returned error"`` while the useful detail — which upstream
provider failed and its raw error — rides on ``exc.body`` under ``error`` /
``error.metadata``. The default ``f"{type(exc).__name__}: {exc}"`` rendering
throws that away. These helpers dig the structured error back out for the screen
(:func:`format_provider_error`), a debug file (:func:`dump_provider_error`), and
the model-actionable note in :mod:`marim_harness.agent`.
"""

from __future__ import annotations

import json
from pathlib import Path


def _find_api_error(exc: BaseException):
    """The first ``openai.APIError`` in ``exc``'s cause/context chain, or None.
    A subagent's provider error reaches the main loop wrapped by the tool layer,
    so we have to look past the outermost exception."""
    try:
        from openai import APIError
    except Exception:  # openai not importable for some reason — nothing to do
        return None
    seen: set[int] = set()
    cur: BaseException | None = exc
    while cur is not None and id(cur) not in seen:
        seen.add(id(cur))
        if isinstance(cur, APIError):
            return cur
        cur = cur.__cause__ or cur.__context__
    return None


def _error_dict(api) -> dict | None:
    """The ``error`` sub-dict OpenRouter nests in the parsed body, or None."""
    body = getattr(api, "body", None)
    err = body.get("error") if isinstance(body, dict) else None
    return err if isinstance(err, dict) else None


def _truncate(text: str, limit: int) -> str:
    return text[: limit - 1] + "…" if len(text) > limit else text


def provider_error_status(exc: BaseException) -> int | None:
    """The HTTP-ish status of a provider error — the SDK's ``status_code`` if
    present, else the ``code`` OpenRouter puts in the body. None when neither is
    a recognizable integer (or the exception isn't a provider error)."""
    api = _find_api_error(exc)
    if api is None:
        return None
    code = getattr(api, "status_code", None)
    if code is None:
        err = _error_dict(api)
        code = err.get("code") if err else None
    if isinstance(code, bool):  # bool is an int subclass — not a status
        return None
    if isinstance(code, int):
        return code
    if isinstance(code, str) and code.isdigit():
        return int(code)
    return None


# Phrases providers use when the request exceeds the model's context window.
# Different upstreams word it differently, so match on a small phrase set in
# addition to OpenAI's explicit ``context_length_exceeded`` error code.
_OVERFLOW_MARKERS = (
    "context_length_exceeded",
    "context length",
    "maximum context",
    "context window",
    "too many tokens",
    "reduce the length",
    "prompt is too long",
    "input is too long",
)


def is_context_overflow_error(exc: BaseException) -> bool:
    """True when ``exc`` is a provider rejection for exceeding the context window.

    The harness's token estimate is a coarse char/4 heuristic, so it can undershoot
    the real window and let a too-large request through; the caller uses this to
    force a compaction and retry instead of failing the turn."""
    api = _find_api_error(exc)
    if api is None:
        return False
    err = _error_dict(api) or {}
    if err.get("code") == "context_length_exceeded":
        return True
    haystack = [str(api), str(err.get("message") or "")]
    meta = err.get("metadata")
    if isinstance(meta, dict):
        haystack.append(str(meta.get("raw") or ""))
    blob = " ".join(haystack).lower()
    return any(marker in blob for marker in _OVERFLOW_MARKERS)


def format_provider_error(exc: BaseException) -> str | None:
    """A one-line, screen-safe rendering of a provider error that pulls in the
    upstream message, code, provider name, and raw detail. None when ``exc``
    isn't a provider error with a structured body — the caller then keeps its own
    ``f"{type(exc).__name__}: {exc}"`` fallback."""
    api = _find_api_error(exc)
    if api is None:
        return None
    err = _error_dict(api)
    if err is None:
        return None
    bits = [f"Provider error: {err.get('message') or str(api)}"]
    code = err.get("code")
    if code is None:
        code = getattr(api, "status_code", None)
    if code is not None:
        bits.append(f"code={code}")
    meta = err.get("metadata")
    if isinstance(meta, dict):
        if meta.get("provider_name"):
            bits.append(f"provider={meta['provider_name']}")
        raw = meta.get("raw")
        if raw:
            bits.append(f"raw={_truncate(str(raw), 300)}")
    return " · ".join(bits)


def provider_error_payload(exc: BaseException) -> dict | None:
    """The full, untruncated provider error as a JSON-serializable dict for
    logging — type, message, and the raw parsed body. None when ``exc`` isn't a
    provider error."""
    api = _find_api_error(exc)
    if api is None:
        return None
    return {
        "type": type(api).__name__,
        "message": str(api),
        "status_code": getattr(api, "status_code", None),
        "body": getattr(api, "body", None),
    }


def dump_provider_error(workspace_root: Path, exc: BaseException) -> Path | None:
    """Write the full provider error payload to ``.marim/last-provider-error.json``
    so the complete upstream detail survives the truncated on-screen view.
    Returns the path written, or None when ``exc`` isn't a provider error."""
    payload = provider_error_payload(exc)
    if payload is None:
        return None
    out = Path(workspace_root) / ".marim" / "last-provider-error.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(payload, indent=2, default=str))
    return out
