"""Public Claude 2.1.270 system shapes: synthetic fixtures, no live CLI."""

import pytest
from pydantic_ai.models import ModelRequestParameters

from marim_harness.claude.lifecycle import ClaudeLifecycle
from marim_harness.config.context_report import ContextReport, current_context_report
from marim_harness.config.lifecycle import BackendNotice, notice_from_part
from marim_harness.runtime.cli_activity import expand_cli_activity
from tests.test_claude_cli_model import _model, _user


def test_compaction_uses_nested_metadata_and_occurrence_identity():
    state = ClaudeLifecycle()
    obj = {
        "subtype": "compact_boundary",
        "uuid": "event",
        "session_id": "session",
        "compact_metadata": {"trigger": "auto", "pre_tokens": 900, "post_tokens": 200},
    }
    [notice] = state.consume(obj)
    assert isinstance(notice, BackendNotice)
    assert notice.kind == "compaction"
    assert notice.data == {"pre_tokens": 900, "post_tokens": 200, "trigger": "auto"}
    assert notice.id == "claude:session:event"
    assert state.consume(obj) == []


def test_internal_fallback_and_malformed_events_are_ignored():
    state = ClaudeLifecycle()
    assert state.consume({"subtype": "model_fallback", "content": "private"}) == []
    assert state.consume({"subtype": "compact_boundary", "compact_metadata": []}) == []
    assert state.consume({"subtype": "unknown"}) == []
    assert state.consume({"subtype": "notification", "text": {"secret": 1}}) == []


def test_public_refusal_fallback_names_models():
    [notice] = ClaudeLifecycle().consume(
        {
            "subtype": "model_refusal_fallback",
            "original_model": "one",
            "fallback_model": "two",
            "direction": "retry",
        }
    )
    assert notice.message == "Claude model fallback: one → two"
    assert notice.kind == "model_refusal_fallback"


def test_telemetry_and_inventory_replace_without_billing():
    state = ClaudeLifecycle()
    assert state.consume({"subtype": "thinking_tokens", "estimated_tokens": 42}) == []
    assert state.thinking_tokens == 42
    assert state.consume({"subtype": "thinking_tokens", "estimated_tokens": True}) == []
    assert state.thinking_tokens == 42
    state.consume({"subtype": "session_state_changed", "state": "requires_action"})
    assert state.backend_state == "requires_action"
    state.consume(
        {
            "subtype": "init",
            "tools": ["Read", 1],
            "slash_commands": ["help"],
            "mcp_servers": [{"name": "docs", "status": "connected"}],
        }
    )
    assert state.inventory["tools"] == ["Read"]
    assert state.inventory["mcp_servers"] == [{"name": "docs", "status": "connected"}]
    state.consume({"subtype": "init", "tools": ["Bash"]})
    assert state.inventory["tools"] == ["Bash"]
    assert state.inventory["slash_commands"] == []


def test_shell_lifecycle_does_not_claim_agents_and_closes_running_tasks():
    state = ClaudeLifecycle()
    assert (
        state.consume({"subtype": "task_started", "task_id": "agent", "task_type": "local_agent"})
        == []
    )
    [started] = state.consume(
        {
            "subtype": "task_started",
            "session_id": "s",
            "task_id": "shell",
            "task_type": "local_bash",
            "description": "Build",
        }
    )
    assert started.task_id == "claude:s:shell" and started.status == "running"
    [progress] = state.consume(
        {"subtype": "task_progress", "task_id": "shell", "description": "Still building"}
    )
    assert progress.task_id == started.task_id and progress.status == "running"
    [finished] = state.consume(
        {
            "subtype": "task_notification",
            "task_id": "shell",
            "status": "completed",
            "summary": "Built",
        }
    )
    assert finished.task_id == started.task_id and finished.status == "completed"
    assert (
        state.consume({"subtype": "task_notification", "task_id": "shell", "status": "completed"})
        == []
    )
    state.consume({"subtype": "task_started", "task_id": "other", "task_type": "local_bash"})
    [closed] = state.close()
    assert closed.status == "interrupted"


def test_result_details_are_normalized_not_raw_tool_inputs():
    state = ClaudeLifecycle()
    state.result(
        {
            "num_turns": 3,
            "duration_api_ms": 40,
            "stop_reason": "end_turn",
            "permission_denials": [
                {"tool_name": "Bash", "tool_use_id": "t", "tool_input": "secret"}
            ],
        }
    )
    assert state.result_details == {
        "num_turns": 3,
        "duration_api_ms": 40,
        "stop_reason": "end_turn",
        "permission_denials": [{"tool_name": "Bash", "tool_use_id": "t"}],
    }


@pytest.mark.anyio
async def test_fake_stream_delivers_notice_before_finish_and_persists_order(tmp_path, monkeypatch):
    model = _model(
        tmp_path,
        monkeypatch,
        {
            "turns": [
                [
                    {"text": "before"},
                    {
                        "raw": {
                            "type": "system",
                            "subtype": "compact_boundary",
                            "uuid": "compact",
                            "session_id": "s",
                            "compact_metadata": {"pre_tokens": 1000, "post_tokens": 100},
                        }
                    },
                    {"text": "after"},
                ]
            ]
        },
    )
    seen = []

    async def activity(events):
        for event in events:
            if isinstance(event, BackendNotice):
                seen.append((event.message, model.context_report))

    model.on_activity = activity
    try:
        async with model.request_stream(_user("hello"), None, ModelRequestParameters()) as stream:
            async for _ in stream:
                pass
            assert seen == [("Claude compacted its context", ContextReport(100, None))]
            response = stream.get()
        parts = [p for m in expand_cli_activity([response]) for p in m.parts]
        assert [p.content for p in parts] == ["before", "", "after"]
        assert notice_from_part(parts[1])["id"] == "claude:s:compact"
        assert response.metadata["backend_result"]["num_turns"] == 1
    finally:
        await model.aclose()


def test_missing_post_compaction_count_invalidates_persisted_fallback():
    from pydantic_ai.messages import ModelResponse

    from marim_harness.claude.lifecycle import LifecycleChunk
    from marim_harness.config.claude_cli_model import ClaudeCliModel

    model = ClaudeCliModel("default")
    model.context_report = ContextReport(900, 1000)
    history = [
        ModelResponse(
            parts=[],
            provider_name="claude-cli",
            provider_details={"cli_context": {"used": 800, "window": 1000}},
        )
    ]
    model._consume_lifecycle(
        LifecycleChunk({"subtype": "compact_boundary", "compact_metadata": {"pre_tokens": 900}})
    )
    assert model.context_report is None
    assert current_context_report(model, history) is None


@pytest.mark.anyio
async def test_closing_process_settles_live_shell_card(tmp_path, monkeypatch):
    from marim_harness.claude.lifecycle import BackendTask

    model = _model(
        tmp_path,
        monkeypatch,
        {
            "turns": [
                [
                    {
                        "raw": {
                            "type": "system",
                            "subtype": "task_started",
                            "task_type": "local_bash",
                            "session_id": "s",
                            "task_id": "shell",
                            "description": "Build",
                        }
                    },
                    {"text": "Started"},
                ]
            ]
        },
    )
    seen = []

    async def activity(events):
        seen.extend(event for event in events if isinstance(event, BackendTask))

    model.on_activity = activity
    async with model.request_stream(_user("go"), None, ModelRequestParameters()) as stream:
        async for _ in stream:
            pass
    await model.aclose()
    assert [(e.task_id, e.status) for e in seen] == [
        ("claude:s:shell", "running"),
        ("claude:s:shell", "interrupted"),
    ]


def test_compaction_preserves_window_and_fresh_usage_restores_unknown_reading():
    from marim_harness.claude.lifecycle import LifecycleChunk
    from marim_harness.config.claude_cli_model import ClaudeCliModel, PromptUsageChunk

    model = ClaudeCliModel("default")
    model.context_report = ContextReport(900, 1000)
    model._consume_lifecycle(
        LifecycleChunk({"subtype": "compact_boundary", "compact_metadata": {"post_tokens": 100}})
    )
    assert model.context_report == ContextReport(100, 1000)
    model._consume_lifecycle(
        LifecycleChunk({"subtype": "compact_boundary", "compact_metadata": {}})
    )
    assert current_context_report(model, []) is None
    model._note(PromptUsageChunk(120))
    assert model.context_report == ContextReport(120, 1000)
    assert current_context_report(model, []) == ContextReport(120, 1000)


@pytest.mark.anyio
async def test_unexpected_process_failure_settles_shell_as_failed(tmp_path, monkeypatch):
    from marim_harness.claude.lifecycle import BackendTask
    from marim_harness.config.external_cli import CliModelError

    model = _model(
        tmp_path,
        monkeypatch,
        {
            "turns": [
                [
                    {
                        "raw": {
                            "type": "system",
                            "subtype": "task_started",
                            "task_type": "local_bash",
                            "session_id": "s",
                            "task_id": "job",
                        }
                    },
                    {"exit": {"code": 7}},
                ]
            ]
        },
    )
    seen = []

    async def activity(events):
        seen.extend(event for event in events if isinstance(event, BackendTask))

    model.on_activity = activity
    try:
        with pytest.raises(CliModelError):
            async with model.request_stream(_user("go"), None, ModelRequestParameters()) as stream:
                async for _ in stream:
                    pass
    finally:
        await model.aclose()
    assert [(event.task_id, event.status) for event in seen] == [
        ("claude:s:job", "running"),
        ("claude:s:job", "failed"),
    ]
