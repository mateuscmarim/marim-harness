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
from pydantic_ai.messages import (
    ModelRequest,
    ModelResponse,
    TextPart,
    UserPromptPart,
)
from pydantic_ai.models.function import FunctionModel

from marim_harness.agent import Harness, HarnessConfig, _actionable_error_note
from marim_harness.deps import Deps
from marim_harness.errors import (
    dump_provider_error,
    format_provider_error,
    is_context_overflow_error,
    provider_error_payload,
)
from marim_harness.permissions import Mode
from marim_harness.tools.provider import BuiltinToolProvider


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
        deps=Deps(workspace_root=tmp_path, mode=Mode.auto),
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
        deps=Deps(workspace_root=tmp_path, mode=Mode.auto),
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
async def test_run_turn_dumps_provider_error_and_stashes_note(tmp_path):
    # A provider error during a turn must leave the full payload on disk for the
    # user and an actionable note for the model's next turn — even though the
    # turn still re-raises to the UI.
    def fn(messages, info):
        raise _api_error({"error": {"message": "invalid request", "code": 400}})

    harness = Harness(
        model=FunctionModel(fn),
        provider=BuiltinToolProvider(),
        deps=Deps(workspace_root=tmp_path, mode=Mode.auto),
        instructions="You are a coding agent.",
    )
    with pytest.raises(APIError):
        await harness.run_turn("hi")

    dump = tmp_path / ".marim" / "last-provider-error.json"
    assert dump.exists()
    assert "invalid request" in dump.read_text()
    assert harness._pending_error_note is not None
    assert "400" in harness._pending_error_note
