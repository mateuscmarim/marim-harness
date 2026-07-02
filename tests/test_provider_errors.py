"""Surfacing the real provider (OpenRouter) error.

A failed turn used to render as ``APIError: Provider returned error`` — the
``openai.APIError`` class name plus OpenRouter's terse top-level message — while
the useful detail (the upstream provider name and its raw error) sat unread on
``exc.body``. These tests pin the extraction, display formatting, raw-body dump,
and model-actionable note so the detail actually reaches the screen, a debug
file, and (when it's the model's to fix) the next turn.
"""

import json

import httpx
import pytest
from openai import APIError
from pydantic_ai.exceptions import ModelHTTPError
from pydantic_ai.messages import (
    ModelRequest,
    ModelResponse,
    TextPart,
    ToolCallPart,
    UserPromptPart,
)
from pydantic_ai.models.function import FunctionModel

from marim_harness.runtime.errors import (
    dump_provider_error,
    format_provider_error,
    is_context_overflow_error,
    is_transient_model_error,
    provider_error_payload,
)
from marim_harness.runtime.harness import Harness, HarnessConfig, _actionable_error_note
from marim_harness.tools.provider import BuiltinToolProvider
from tests.conftest import _make_deps


def _api_error(body):
    """An ``openai.APIError`` shaped like one OpenRouter raises, with ``body``
    carrying the structured error the SDK parsed off the response."""
    req = httpx.Request("POST", "https://openrouter.ai/api/v1/chat/completions")
    return APIError("Provider returned error", req, body=body)


_OPENROUTER_502 = {
    "error": {
        "message": "Provider returned error",
        "code": 502,
        "metadata": {"provider_name": "Xiaomi", "raw": "upstream timeout"},
    }
}


# --- is_transient_model_error -------------------------------------------------


def _http_error(status: int, body=None) -> ModelHTTPError:
    return ModelHTTPError(status_code=status, model_name="xiaomi/mimo-v2.5", body=body)


@pytest.mark.parametrize("status", [408, 409, 429, 500, 502, 503, 504])
def test_gateway_and_rate_limit_statuses_are_transient(status):
    assert is_transient_model_error(_http_error(status)) is True


def test_504_idle_timeout_is_transient():
    # The exact shape from the failing review fan-out: a 504 the runner should
    # retry rather than burn the whole sub-agent run on.
    exc = _http_error(504, body="Upstream idle timeout exceeded")
    assert is_transient_model_error(exc) is True


@pytest.mark.parametrize("status", [401, 403, 404])
def test_auth_and_not_found_are_not_transient(status):
    assert is_transient_model_error(_http_error(status)) is False


def test_genuine_400_bad_request_is_not_transient():
    body = {"message": "invalid request: unsupported parameter 'foo'", "code": 400}
    assert is_transient_model_error(_http_error(400, body)) is False


def test_openrouter_400_wrapping_an_upstream_5xx_is_transient():
    # OpenRouter passes an upstream failure through as a generic 400; the real
    # status rides in metadata.raw as a JSON string.
    body = {
        "message": "Provider returned error",
        "code": 400,
        "metadata": {"raw": '{"error":{"code":503,"message":"overloaded"}}'},
    }
    assert is_transient_model_error(_http_error(400, body)) is True


def test_openrouter_400_with_transient_phrase_is_transient():
    body = {
        "message": "Provider returned error",
        "code": 400,
        "metadata": {"raw": "upstream request timed out, please try again"},
    }
    assert is_transient_model_error(_http_error(400, body)) is True


def test_non_http_exception_is_not_transient():
    assert is_transient_model_error(ValueError("boom")) is False


def test_transient_error_wrapped_in_a_cause_chain_is_detected():
    inner = _http_error(503)
    outer = RuntimeError("sub-agent failed")
    outer.__cause__ = inner
    assert is_transient_model_error(outer) is True


def test_format_extracts_message_code_provider_and_raw():
    out = format_provider_error(_api_error(_OPENROUTER_502))
    assert out is not None
    assert "Provider returned error" in out
    assert "502" in out
    assert "Xiaomi" in out
    assert "upstream timeout" in out


def test_format_returns_none_for_plain_exception():
    # Not a provider error — caller keeps its own "{type}: {exc}" fallback.
    assert format_provider_error(ValueError("boom")) is None


def test_format_unwraps_exception_chain():
    # A subagent's provider error reaches run_turn wrapped by the tool layer;
    # the detail must still be found through __cause__.
    inner = _api_error({"error": {"message": "Provider returned error"}})
    try:
        try:
            raise inner
        except Exception as cause:
            raise RuntimeError("spawn_agent failed") from cause
    except RuntimeError as outer:
        out = format_provider_error(outer)
    assert out is not None
    assert "Provider returned error" in out


def test_format_without_metadata_still_includes_message_and_code():
    out = format_provider_error(_api_error({"error": {"message": "Bad request", "code": 400}}))
    assert out is not None
    assert "Bad request" in out
    assert "400" in out


def test_payload_returns_type_and_raw_body_for_logging():
    payload = provider_error_payload(_api_error(_OPENROUTER_502))
    assert payload is not None
    assert payload["type"] == "APIError"
    assert payload["body"] == _OPENROUTER_502


def test_payload_none_for_plain_exception():
    assert provider_error_payload(ValueError("boom")) is None


def test_dump_writes_full_raw_body_to_marim_file(tmp_path):
    path = dump_provider_error(tmp_path, _api_error(_OPENROUTER_502))
    assert path is not None
    assert path == tmp_path / ".marim" / "last-provider-error.json"
    data = json.loads(path.read_text())
    assert data["body"] == _OPENROUTER_502
    # The full upstream detail is preserved verbatim, not truncated like the UI.
    assert "upstream timeout" in path.read_text()


def test_dump_returns_none_for_plain_exception(tmp_path):
    assert dump_provider_error(tmp_path, ValueError("boom")) is None
    assert not (tmp_path / ".marim" / "last-provider-error.json").exists()


def test_actionable_note_for_provider_client_error():
    # A 4xx (non-429) provider rejection is the model's to fix — it gets a note.
    note = _actionable_error_note(
        _api_error({"error": {"message": "invalid request", "code": 400}})
    )
    assert note is not None
    assert "400" in note


def test_no_actionable_note_for_provider_5xx():
    # A 5xx is transient infra; re-prompting would only mislead the model.
    assert _actionable_error_note(_api_error(_OPENROUTER_502)) is None


def test_is_context_overflow_detects_openai_code():
    err = _api_error(
        {"error": {"code": "context_length_exceeded", "message": "too long"}}
    )
    assert is_context_overflow_error(err) is True


def test_is_context_overflow_detects_message_phrase():
    err = _api_error(
        {"error": {"message": "This model's maximum context length is 8192 tokens"}}
    )
    assert is_context_overflow_error(err) is True


def test_is_context_overflow_false_for_other_provider_and_plain_errors():
    assert is_context_overflow_error(_api_error(_OPENROUTER_502)) is False
    assert is_context_overflow_error(ValueError("boom")) is False


@pytest.mark.anyio
async def test_run_turn_force_compacts_and_retries_on_context_overflow(tmp_path):
    """The char/4 estimate can undershoot the real window. When the provider
    rejects the request for length, force a compaction and retry once instead of
    failing the turn."""
    calls = {"n": 0}

    def fn(messages, info):
        calls["n"] += 1
        if calls["n"] == 1:
            raise _api_error(
                {"error": {"code": "context_length_exceeded",
                           "message": "maximum context length exceeded"}}
            )
        return ModelResponse(parts=[TextPart(content="ok after compaction")])

    harness = Harness(
        model=FunctionModel(fn),
        provider=BuiltinToolProvider(),
        deps=_make_deps(tmp_path),
        instructions="x",
        config=HarnessConfig(keep_last_messages=1),
    )
    # A multi-turn history so a forced compaction has a middle to drop.
    harness.session.history = [
        ModelRequest(parts=[UserPromptPart(content="u1")]),
        ModelResponse(parts=[TextPart(content="a1")]),
        ModelRequest(parts=[UserPromptPart(content="u2")]),
        ModelResponse(parts=[TextPart(content="a2")]),
        ModelRequest(parts=[UserPromptPart(content="u3")]),
        ModelResponse(parts=[TextPart(content="a3")]),
    ]
    out = await harness.run_turn("now do it")
    assert out == "ok after compaction"
    assert calls["n"] == 2  # failed once on overflow, retried once after compaction


@pytest.mark.anyio
async def test_run_turn_overflow_retries_only_once(tmp_path):
    """If the request still overflows after a forced compaction, the turn raises
    rather than looping forever."""
    calls = {"n": 0}

    def fn(messages, info):
        calls["n"] += 1
        raise _api_error(
            {"error": {"code": "context_length_exceeded", "message": "too long"}}
        )

    harness = Harness(
        model=FunctionModel(fn),
        provider=BuiltinToolProvider(),
        deps=_make_deps(tmp_path),
        instructions="x",
        config=HarnessConfig(keep_last_messages=1),
    )
    harness.session.history = [
        ModelRequest(parts=[UserPromptPart(content="u1")]),
        ModelResponse(parts=[TextPart(content="a1")]),
        ModelRequest(parts=[UserPromptPart(content="u2")]),
        ModelResponse(parts=[TextPart(content="a2")]),
        ModelRequest(parts=[UserPromptPart(content="u3")]),
        ModelResponse(parts=[TextPart(content="a3")]),
    ]
    with pytest.raises(APIError):
        await harness.run_turn("now do it")
    assert calls["n"] == 2  # original attempt + exactly one retry


@pytest.mark.anyio
async def test_run_turn_accumulates_partial_usage_on_provider_error(tmp_path):
    """Tokens spent before a mid-run provider failure must still land in
    ``session.usage``. The first model step (which emitted a tool call) was
    really billed; the second step's failure can't silently drop that usage from
    the running total."""
    (tmp_path / "f.txt").write_text("hello")
    calls = {"n": 0}

    def fn(messages, info):
        calls["n"] += 1
        if calls["n"] == 1:
            return ModelResponse(
                parts=[ToolCallPart(tool_name="read_file", args={"path": "f.txt"})]
            )
        raise _api_error(_OPENROUTER_502)

    harness = Harness(
        model=FunctionModel(fn),
        provider=BuiltinToolProvider(),
        deps=_make_deps(tmp_path),
        instructions="x",
    )
    with pytest.raises(APIError):
        await harness.run_turn("read it")
    # The first call's usage must survive the second call's failure.
    assert harness.session.usage.requests >= 1
    assert harness.session.usage.input_tokens > 0


@pytest.mark.anyio
async def test_run_turn_dumps_provider_error_and_stashes_note(tmp_path):
    # A provider error during a turn must leave the full payload on disk for the
    # user and an actionable note for the model's next turn — even though the
    # turn still re-raises to the UI.
    def fn(messages, info):
        raise _api_error({"error": {"message": "invalid request", "code": 400}})

    harness = Harness(
        model=FunctionModel(fn),
        provider=BuiltinToolProvider(),
        deps=_make_deps(tmp_path),
        instructions="You are a coding agent.",
    )
    with pytest.raises(APIError):
        await harness.run_turn("hi")

    dump = tmp_path / ".marim" / "last-provider-error.json"
    assert dump.exists()
    assert "invalid request" in dump.read_text()
    assert harness.turn_controller._pending_error_note is not None
    assert "400" in harness.turn_controller._pending_error_note


def test_is_context_overflow_detects_model_http_error_body():
    """A sub-agent's model layer surfaces the provider rejection as a pydantic-ai
    ModelHTTPError (no openai.APIError in the chain). The detector must classify
    it, or the runner's shed-and-resume backstop never fires."""
    err = ModelHTTPError(
        400, "m",
        body={"message": "This model's maximum context length is 8192 tokens."},
    )
    assert is_context_overflow_error(err) is True


def test_is_context_overflow_model_http_error_plain_400_is_false():
    """A genuine bad request must NOT read as an overflow — the backstop would
    mask-and-resume a request that will fail identically."""
    err = ModelHTTPError(
        400, "m", body={"message": "invalid request: unsupported parameter"}
    )
    assert is_context_overflow_error(err) is False


def test_marker_phrase_on_a_transient_status_is_not_an_overflow():
    """A 429/5xx body can mention an overflow marker phrase in prose ("context
    window", "reduce the length") without the request being oversized. The
    runner checks overflow BEFORE the transient classifier, so an unguarded
    match would shed a sub-agent's context on a hiccup that a plain backoff
    retry would fix — the error must classify transient, not overflow."""
    err = ModelHTTPError(
        503, "m",
        body={"message": "The context window service is temporarily overloaded; "
                         "please try again."},
    )
    assert is_context_overflow_error(err) is False
    assert is_transient_model_error(err) is True


def test_overflow_marker_on_413_still_classifies_as_overflow():
    # Some providers reject an oversized request with 413 Payload Too Large.
    err = ModelHTTPError(413, "m", body={"message": "prompt is too long"})
    assert is_context_overflow_error(err) is True
