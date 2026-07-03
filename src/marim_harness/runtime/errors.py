"""Surface the *real* provider error behind a failed turn.

OpenRouter (via the OpenAI SDK) raises ``openai.APIError`` with a terse message
like ``"Provider returned error"`` while the useful detail — which upstream
provider failed and its raw error — rides on ``exc.body`` under ``error`` /
``error.metadata``. The default ``f"{type(exc).__name__}: {exc}"`` rendering
throws that away. These helpers dig the structured error back out for the screen
(:func:`format_provider_error`), a debug file (:func:`dump_provider_error`), and
the model-actionable note in :mod:`marim_harness.runtime.harness`.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

from ..atomic_io import atomic_write_text


def _find_in_chain(exc: BaseException, exc_class):
    """Walk ``exc``'s ``__cause__``/``__context__`` chain; return the first
    instance of ``exc_class`` found, or None."""
    seen: set[int] = set()
    cur: BaseException | None = exc
    while cur is not None and id(cur) not in seen:
        seen.add(id(cur))
        if isinstance(cur, exc_class):
            return cur
        cur = cur.__cause__ or cur.__context__
    return None


def _find_api_error(exc: BaseException):
    """The first ``openai.APIError`` in ``exc``'s cause/context chain, or None.
    A subagent's provider error reaches the main loop wrapped by the tool layer,
    so we have to look past the outermost exception."""
    try:
        from openai import APIError
    except Exception:  # openai not importable for some reason — nothing to do
        return None
    return _find_in_chain(exc, APIError)


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
    # LM Studio (the `local` provider): "Context size has been exceeded." —
    # no status code, no nested error dict, just this message. Missing from
    # this set, it once let a whole research fan-out die un-recovered.
    "context size",
    "too many tokens",
    "reduce the length",
    "prompt is too long",
    "input is too long",
)


# HTTP statuses worth retrying: gateway/server hiccups, request timeouts,
# conflicts, and rate limits. Re-issuing the identical request commonly succeeds.
_TRANSIENT_STATUS = frozenset({408, 409, 429, 500, 502, 503, 504})

# Phrases an upstream uses for a transient condition even when OpenRouter has
# wrapped it in a generic 400 "Provider returned error" — the real cause then
# rides in the body's message / metadata.raw passthrough. These are distinctive
# words, so a plain substring match is safe (none collide with unrelated text).
_TRANSIENT_PHRASES = (
    "timeout",
    "timed out",
    "overloaded",
    "rate limit",
    "rate-limit",
    "temporarily",
    "try again",
    "unavailable",
)

# A transient HTTP status (502/503/504) named *in prose* — e.g. "upstream
# returned 503" or "HTTP 502 Bad Gateway". Matched with a regex rather than a
# bare substring so we don't misread an unrelated number that happens to contain
# those digits (a byte count like 65029, a request id "req-503812", a timestamp).
# Two precise shapes count: (a) a status word ("status"/"code"/"http"/"error")
# within a few chars before the code, or (b) the code immediately followed by its
# canonical reason phrase. Both require the three digits to stand alone as a token
# (no surrounding digits), which alone already rules out the "65029" class of
# false positives.
_TRANSIENT_STATUS_RE = re.compile(
    r"(?:\b(?:status|code|http|error)\b[^0-9]{0,8}(?:50[234])(?![0-9]))"
    r"|(?:\b(?:50[234])\b\s*(?:bad gateway|service unavailable|gateway time))"
)


def _text_signals_transient(haystack: str) -> bool:
    """Whether ``haystack`` (already lowercased) names a transient condition: a
    distinctive transient phrase, or a 502/503/504 status named in prose. Kept
    precise so unrelated numbers/text don't trigger a spurious retry."""
    if any(phrase in haystack for phrase in _TRANSIENT_PHRASES):
        return True
    return _TRANSIENT_STATUS_RE.search(haystack) is not None


def _find_model_http_error(exc: BaseException):
    """The first pydantic-ai ``ModelHTTPError`` in ``exc``'s cause/context chain,
    or None. A sub-agent's model error reaches the runner wrapped by the agent
    layer, so we look past the outermost exception (mirrors ``_find_api_error``)."""
    try:
        from pydantic_ai.exceptions import ModelHTTPError
    except Exception:  # pydantic_ai not importable — nothing to classify
        return None
    return _find_in_chain(exc, ModelHTTPError)


def _status_codes_in(obj) -> list[int]:
    """Every ``code``/``status``/``status_code`` integer nested anywhere in ``obj``
    (a parsed upstream error body). Used to see past OpenRouter's generic 400 to the
    real upstream status it forwarded."""
    codes: list[int] = []

    def walk(node) -> None:
        if isinstance(node, dict):
            for key, val in node.items():
                if (
                    key in ("code", "status", "status_code")
                    and isinstance(val, int)
                    and not isinstance(val, bool)
                ):
                    codes.append(val)
                else:
                    walk(val)
        elif isinstance(node, list):
            for item in node:
                walk(item)

    walk(obj)
    return codes


def _body_signals_transient(body) -> bool:
    """Whether an OpenRouter 400/422 body hides a transient upstream cause: a nested
    5xx/timeout/429 status in the forwarded ``metadata.raw``, or a transient phrase
    in the message / raw text. A genuinely malformed request matches neither."""
    raw = None
    if isinstance(body, dict):
        meta = body.get("metadata")
        if isinstance(meta, dict):
            raw = meta.get("raw")
    # metadata.raw is forwarded verbatim from the upstream — often a JSON string.
    candidate = raw
    if isinstance(raw, str):
        try:
            candidate = json.loads(raw)
        except (ValueError, TypeError):
            candidate = None
    if any(code in _TRANSIENT_STATUS for code in _status_codes_in(candidate)):
        return True
    message = body.get("message") if isinstance(body, dict) else None
    haystack = " ".join([str(message or ""), str(raw or "")]).lower()
    return _text_signals_transient(haystack)


def is_transient_model_error(exc: BaseException) -> bool:
    """True when ``exc`` is a model/provider error worth retrying — a gateway or
    server hiccup, a request timeout, or a rate limit — rather than a permanent
    client error (malformed request, auth, not-found).

    Reads the status off a pydantic-ai ``ModelHTTPError``. OpenRouter overloads 400
    as a passthrough for upstream provider failures, so a 400/422 is inspected: a
    nested transient upstream status or a transient phrase in its body counts as
    transient; an otherwise-genuine bad request does not. A non-HTTP exception (no
    ``ModelHTTPError`` in the chain) is treated as permanent — better to fail fast
    than hammer on something we can't positively identify as a transient blip."""
    api = _find_model_http_error(exc)
    if api is None:
        return False
    status = getattr(api, "status_code", None)
    if status in _TRANSIENT_STATUS:
        return True
    if status in (400, 422):
        return _body_signals_transient(getattr(api, "body", None))
    return False


def is_context_overflow_error(exc: BaseException) -> bool:
    """True when ``exc`` is a provider rejection for exceeding the context window.

    The harness's token estimate is a coarse char/4 heuristic, so it can undershoot
    the real window and let a too-large request through; the caller uses this to
    force a compaction (main turn) or an observation shed (sub-agent) and retry
    instead of failing outright.

    Two shapes are recognized: an ``openai.APIError`` in the chain (the main
    turn's shape — OpenRouter nests the detail in the body), and a pydantic-ai
    ``ModelHTTPError`` (the shape a sub-agent's model layer raises, which may not
    chain an openai error at all)."""
    api = _find_api_error(exc)
    if api is not None:
        err = _error_dict(api) or {}
        if err.get("code") == "context_length_exceeded":
            return True
        # Marker-phrase matching is status-gated like the ModelHTTPError branch
        # below: a 429/5xx message can mention "context window" in prose (a
        # rate limit advising a smaller request, an upstream quote) without the
        # request being oversized, and an unguarded match here force-compacts on
        # a hiccup a plain backoff retry fixes. The status rides either on the
        # exception (APIStatusError) or as an int code in the body; a shape with
        # no status at all (LM Studio's plain APIError) stays eligible — its
        # message is the only signal there is.
        status = getattr(api, "status_code", None)
        if status is None:
            code = err.get("code")
            status = code if isinstance(code, int) else None
        if status is not None and status not in (400, 413, 422):
            return False
        haystack = [str(api), str(err.get("message") or "")]
        meta = err.get("metadata")
        if isinstance(meta, dict):
            haystack.append(str(meta.get("raw") or ""))
        if any(m in " ".join(haystack).lower() for m in _OVERFLOW_MARKERS):
            return True
    http = _find_model_http_error(exc)
    if http is None:
        return False
    # Only the client-error statuses a provider actually uses for an oversized
    # request count (400 bad request, 413 payload too large, 422 unprocessable).
    # A 429/5xx body can mention a marker phrase in prose ("context window",
    # "reduce the length") without the request being oversized — and the runner
    # checks overflow BEFORE the transient classifier, so an unguarded match
    # would shed a sub-agent's context on a hiccup a plain backoff retry fixes.
    if getattr(http, "status_code", None) not in (400, 413, 422):
        return False
    blob = f"{http} {getattr(http, 'body', '') or ''}".lower()
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
    atomic_write_text(out, json.dumps(payload, indent=2, default=str))
    return out
