from __future__ import annotations

from typing import cast

from marim_harness.codex.translate import (
    ActivityEnd,
    ActivityStart,
    ItemTranslator,
    Notice,
    TextDelta,
    ThinkingDelta,
    TurnDone,
    TurnFailure,
    UsageUpdate,
    args_for,
    tool_name_for,
)


def _t() -> ItemTranslator:
    return ItemTranslator()


def test_agent_message_deltas_then_completed_emits_nothing_twice():
    t = _t()
    assert t.translate("item/agentMessage/delta", {"itemId": "m1", "delta": "he"}) == [
        TextDelta("m1", "he")
    ]
    assert t.translate("item/agentMessage/delta", {"itemId": "m1", "delta": "y"}) == [
        TextDelta("m1", "y")
    ]
    done = {"item": {"type": "agentMessage", "id": "m1", "text": "hey"}}
    assert t.translate("item/completed", done) == []


def test_agent_message_completed_without_deltas_emits_full_text():
    t = _t()
    done = {"item": {"type": "agentMessage", "id": "m2", "text": "all at once"}}
    assert t.translate("item/completed", done) == [TextDelta("m2", "all at once")]


def test_reasoning_deltas_and_summary():
    t = _t()
    assert t.translate("item/reasoning/textDelta", {"itemId": "r1", "delta": "th"}) == [
        ThinkingDelta("r1", "th")
    ]
    assert t.translate("item/reasoning/summaryTextDelta", {"itemId": "r1", "delta": "ink"}) == [
        ThinkingDelta("r1", "ink")
    ]
    done = {"item": {"type": "reasoning", "id": "r9", "content": ["a", "b"], "summary": ["s"]}}
    assert t.translate("item/completed", done) == [ThinkingDelta("r9", "s")]


def test_command_execution_lifecycle_buffers_output():
    t = _t()
    started = {
        "item": {
            "type": "commandExecution",
            "id": "c1",
            "command": "ls -la",
            "cwd": "/w",
            "status": "inProgress",
        }
    }
    assert t.translate("item/started", started) == [
        ActivityStart("c1", "bash", {"command": "ls -la", "cwd": "/w"})
    ]
    assert t.translate("item/commandExecution/outputDelta", {"itemId": "c1", "delta": "a\n"}) == []
    assert t.translate("item/commandExecution/outputDelta", {"itemId": "c1", "delta": "b\n"}) == []
    done = {
        "item": {
            "type": "commandExecution",
            "id": "c1",
            "command": "ls -la",
            "status": "completed",
            "exitCode": 0,
            "aggregatedOutput": "ignored",
        }
    }
    [end] = t.translate("item/completed", done)
    assert end == ActivityEnd("c1", "a\nb\n", False)


def test_command_failure_uses_aggregated_output_and_exit_code():
    t = _t()
    done = {
        "item": {
            "type": "commandExecution",
            "id": "c2",
            "command": ["git", "status"],
            "status": "failed",
            "exitCode": 128,
            "aggregatedOutput": "fatal",
        }
    }
    [end] = t.translate("item/completed", done)
    end = cast(ActivityEnd, end)
    assert end.is_error and "fatal" in end.content and "128" in end.content


def test_command_list_form_is_joined():
    item = {"type": "commandExecution", "command": ["git", "commit", "-m", "a b"], "cwd": "/w"}
    assert args_for(item) == {"command": "git commit -m 'a b'", "cwd": "/w"}
    assert tool_name_for(item) == "bash"


def test_file_change_fans_out_per_change():
    t = _t()
    item = {
        "type": "fileChange",
        "id": "f1",
        "status": "inProgress",
        "changes": [
            {"path": "/w/a.py", "kind": {"type": "update"}, "diff": "-x\n+y"},
            {"path": "/w/b.py", "kind": {"type": "add"}, "diff": "+new"},
        ],
    }
    starts = t.translate("item/started", {"item": item})
    assert starts == [
        ActivityStart(
            "f1:0", "apply_patch", {"path": "/w/a.py", "kind": "update", "diff": "-x\n+y"}
        ),
        ActivityStart("f1:1", "apply_patch", {"path": "/w/b.py", "kind": "add", "diff": "+new"}),
    ]
    ends = t.translate("item/completed", {"item": {**item, "status": "completed"}})
    assert ends == [ActivityEnd("f1:0", "-x\n+y", False), ActivityEnd("f1:1", "+new", False)]


def test_args_for_file_change_flattens_a_single_change():
    item = {
        "type": "fileChange",
        "id": "f9",
        "changes": [{"path": "/w/a.py", "kind": {"type": "update"}, "diff": "+x"}],
    }
    assert args_for(item) == {"path": "/w/a.py", "kind": "update", "diff": "+x"}


def test_args_for_file_change_lists_paths_for_multiple_changes():
    item = {
        "type": "fileChange",
        "id": "f9",
        "changes": [
            {"path": "/w/a.py", "kind": {"type": "update"}, "diff": "+x"},
            {"path": "/w/b.py", "kind": {"type": "add"}, "diff": "+y"},
        ],
    }
    result = args_for(item)
    assert result["paths"] == ["/w/a.py", "/w/b.py"]
    assert result["changes"] == [
        {"path": "/w/a.py", "kind": "update", "diff": "+x"},
        {"path": "/w/b.py", "kind": "add", "diff": "+y"},
    ]


def test_mcp_web_search_collab_plan_and_compaction():
    t = _t()
    mcp = {
        "type": "mcpToolCall",
        "id": "t1",
        "server": "gh",
        "tool": "issues",
        "arguments": {"q": 1},
    }
    assert t.translate("item/started", {"item": mcp}) == [
        ActivityStart("t1", "gh.issues", {"q": 1})
    ]
    [end] = t.translate(
        "item/completed",
        {"item": {**mcp, "result": {"ok": True}, "error": None, "status": "completed"}},
    )
    assert end == ActivityEnd("t1", '{"ok": true}', False)
    [end] = t.translate(
        "item/completed",
        {"item": {**mcp, "id": "t2", "error": {"message": "nope"}, "status": "failed"}},
    )
    end = cast(ActivityEnd, end)
    assert end.is_error and "nope" in end.content
    ws = {"type": "webSearch", "id": "w1", "query": "python 3.14"}
    assert t.translate("item/started", {"item": ws}) == [
        ActivityStart("w1", "web_search", {"query": "python 3.14"})
    ]
    collab = {
        "type": "collabAgentToolCall",
        "id": "k1",
        "prompt": "review",
        "model": "gpt-5.4-mini",
        "receiverThreadIds": ["x"],
        "status": "inProgress",
    }
    [start] = t.translate("item/started", {"item": collab})
    start = cast(ActivityStart, start)
    assert start.tool_name == "codex_agent" and start.args["prompt"] == "review"
    plan = {"type": "plan", "id": "p1", "text": "1. do\n2. done"}
    assert t.translate("item/started", {"item": plan}) == [
        ActivityStart("p1", "update_plan", {"text": "1. do\n2. done"}),
        ActivityEnd("p1", "1. do\n2. done", False),
    ]
    assert t.translate("item/started", {"item": {"type": "contextCompaction", "id": "z"}}) == [
        Notice("Codex compacted its context")
    ]
    assert t.translate("item/started", {"item": {"type": "userMessage", "id": "u"}}) == []


def test_turn_level_notifications():
    t = _t()
    usage = {"tokenUsage": {"total": {"inputTokens": 10, "outputTokens": 2}, "last": {}}}
    assert t.translate("thread/tokenUsage/updated", usage) == [
        UsageUpdate({"inputTokens": 10, "outputTokens": 2})
    ]
    # `last` (the newest response's own usage) rides along with the total.
    usage = {"tokenUsage": {"total": {"inputTokens": 10}, "last": {"inputTokens": 4}}}
    assert t.translate("thread/tokenUsage/updated", usage) == [
        UsageUpdate({"inputTokens": 10}, {"inputTokens": 4})
    ]
    assert t.translate(
        "turn/completed", {"turn": {"id": "t", "status": "completed", "error": None}}
    ) == [TurnDone("completed", None)]
    assert t.translate(
        "turn/completed", {"turn": {"status": "failed", "error": {"message": "quota"}}}
    ) == [TurnDone("failed", "quota")]
    assert t.translate("error", {"error": {"message": "rate limited"}, "willRetry": True}) == [
        TurnFailure("rate limited", True)
    ]
    assert t.translate("warning", {"message": "slow"}) == [Notice("slow")]
    assert t.translate("thread/closed", {"threadId": "x"}) == []
