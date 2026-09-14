"""LIFE-26/28/31/32: notices are ordered display metadata, never model prose."""

import pytest
from pydantic_ai.messages import ModelResponse, TextPart

from marim_harness.config.external_cli import CLI_ACTIVITY_KEY, ActivityLedger
from marim_harness.config.lifecycle import BackendNotice, deliver_notice, notice_from_part
from marim_harness.runtime.cli_activity import expand_cli_activity


def test_notice_round_trip_keeps_identity_order_and_prose():
    attached = {}
    ledger = ActivityLedger(lambda entries: attached.update({CLI_ACTIVITY_KEY: entries}))
    ledger.entries.append({"kind": "part", "index": 0})
    first = BackendNotice("Compacted", "claude-cli", "compaction")
    second = BackendNotice("Compacted", "claude-cli", "compaction")
    ledger.note_notice(first)
    ledger.note_notice(second)
    ledger.entries.append({"kind": "part", "index": 1})
    response = ModelResponse(
        parts=[TextPart("before"), TextPart("after")], provider_details=attached
    )
    messages = expand_cli_activity([response])
    parts = [part for message in messages for part in message.parts]
    assert [p.content for p in parts] == ["before", "", "", "after"]
    assert [notice_from_part(p) for p in parts[1:3]] == [first.to_payload(), second.to_payload()]
    assert first.id != second.id
    assert expand_cli_activity(messages) == messages
    assert "".join(p.content for p in parts) == "beforeafter"


@pytest.mark.anyio
async def test_notice_callback_failure_does_not_abort_or_log_payload(caplog):
    async def broken(events):
        raise RuntimeError("private payload")

    await deliver_notice(BackendNotice("private notice", "codex-cli", "warning"), broken)
    assert "codex-cli" in caplog.text
    assert "RuntimeError" in caplog.text
    assert "private payload" not in caplog.text
    assert "private notice" not in caplog.text


@pytest.mark.anyio
async def test_headless_notice_is_stderr_only(capsys):
    await deliver_notice(BackendNotice("Context compacted", "codex-cli", "compaction"), None)
    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err == "Context compacted\n"


def test_malformed_notice_metadata_is_ignored():
    part = TextPart("", provider_name="marim", provider_details={"backend_notice": {"message": 4}})
    assert notice_from_part(part) is None
    assert notice_from_part(TextPart("plain")) is None


def test_notice_between_parallel_results_keeps_both_real_results():
    entries = [
        {"kind": "call", "id": "a", "name": "bash", "args": {}},
        {"kind": "call", "id": "b", "name": "bash", "args": {}},
        {"kind": "result", "id": "a", "content": "first", "outcome": "success"},
        {"kind": "notice", "notice": BackendNotice("note", "codex-cli", "warning").to_payload()},
        {"kind": "result", "id": "b", "content": "second", "outcome": "success"},
    ]
    response = ModelResponse(parts=[], provider_details={CLI_ACTIVITY_KEY: entries})
    messages = expand_cli_activity([response])
    returns = [p for m in messages for p in m.parts if p.part_kind == "tool-return"]
    assert [p.content for p in returns] == ["first", "second"]
    assert returns[0].metadata["backend_notices_after"][0]["message"] == "note"
