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
from pydantic_ai.models.function import FunctionModel

from marim_harness.agent import Harness, _actionable_error_note
from marim_harness.deps import Deps
from marim_harness.permissions import Mode
from marim_harness.tools.provider import BuiltinToolProvider
from marim_harness.errors import (
    dump_provider_error,
    format_provider_error,
    provider_error_payload,
)


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
    note = _actionable_error_note(_api_error({"error": {"message": "invalid request", "code": 400}}))
    assert note is not None
    assert "400" in note


def test_no_actionable_note_for_provider_5xx():
    # A 5xx is transient infra; re-prompting would only mislead the model.
    assert _actionable_error_note(_api_error(_OPENROUTER_502)) is None


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
    with pytest.raises(BaseException):
        await harness.run_turn("hi")

    dump = tmp_path / ".marim" / "last-provider-error.json"
    assert dump.exists()
    assert "invalid request" in dump.read_text()
    assert harness._pending_error_note is not None
    assert "400" in harness._pending_error_note
