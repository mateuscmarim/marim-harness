"""``runtime.cli_activity``: a CLI provider's recorded tool activity becomes
real tool-call / tool-return messages at the controller's persist points."""

from __future__ import annotations

import pytest
from pydantic_ai.messages import (
    ModelRequest,
    ModelResponse,
    TextPart,
    ThinkingPart,
    ToolCallPart,
    ToolReturnPart,
    UserPromptPart,
)
from pydantic_ai.models.function import FunctionModel
from pydantic_ai.usage import RequestUsage

from marim_harness.config.external_cli import CLI_ACTIVITY_KEY
from marim_harness.runtime.cli_activity import MISSING_RESULT_NOTE, expand_cli_activity
from marim_harness.runtime.controller import TurnController
from marim_harness.runtime.harness import HarnessConfig, build_collaborators
from marim_harness.tools.provider import BuiltinToolProvider
from tests.conftest import _make_deps


def _resp(parts, ledger, **kw) -> ModelResponse:
    return ModelResponse(
        parts=parts,
        usage=RequestUsage(input_tokens=5, output_tokens=7),
        provider_details={CLI_ACTIVITY_KEY: ledger, "other": 1},
        model_name="sonnet",
        provider_name="claude-cli",
        **kw,
    )


def _shape(messages) -> list[tuple[str, list[tuple[str, str | None]]]]:
    """(message kind, [(part kind, tool_call_id)]) per message."""
    out = []
    for msg in messages:
        parts = [(p.part_kind, getattr(p, "tool_call_id", None)) for p in msg.parts]
        out.append((msg.kind, parts))
    return out


def test_sequential_calls_split_into_call_return_response_chain():
    ledger = [
        {"kind": "part", "index": 0},
        {"kind": "call", "id": "t1", "name": "read_file", "args": {"path": "a.py"}},
        {"kind": "result", "id": "t1", "content": "print(1)", "outcome": "success"},
        {"kind": "part", "index": 1},
        {"kind": "call", "id": "t2", "name": "bash", "args": {"command": "ls"}},
        {"kind": "result", "id": "t2", "content": "boom", "outcome": "failed"},
        {"kind": "part", "index": 2},
    ]
    src = _resp([TextPart(content="A"), TextPart(content="B"), TextPart(content="C")], ledger)
    user = ModelRequest(parts=[UserPromptPart(content="hi")])
    out = expand_cli_activity([user, src])
    assert out[0] is user
    assert _shape(out[1:]) == [
        ("response", [("text", None), ("tool-call", "t1")]),
        ("request", [("tool-return", "t1")]),
        ("response", [("text", None), ("tool-call", "t2")]),
        ("request", [("tool-return", "t2")]),
        ("response", [("text", None)]),
    ]
    call = out[1].parts[1]
    assert isinstance(call, ToolCallPart) and call.tool_name == "read_file"
    assert call.args == {"path": "a.py"}
    ret = out[4].parts[0]
    assert isinstance(ret, ToolReturnPart)
    assert ret.tool_name == "bash" and ret.content == "boom" and ret.outcome == "failed"
    assert out[2].parts[0].content == "print(1)"


def test_parallel_calls_share_one_response_and_one_answering_request():
    ledger = [
        {"kind": "part", "index": 0},
        {"kind": "part", "index": 1},
        {"kind": "call", "id": "a", "name": "read_file", "args": {}},
        {"kind": "call", "id": "b", "name": "read_file", "args": {}},
        {"kind": "result", "id": "a", "content": "ra", "outcome": "success"},
        {"kind": "result", "id": "b", "content": "rb", "outcome": "success"},
        {"kind": "part", "index": 2},
    ]
    src = _resp(
        [ThinkingPart(content="hm"), TextPart(content="Look."), TextPart(content="Done.")], ledger
    )
    out = expand_cli_activity([src])
    assert _shape(out) == [
        ("response", [("thinking", None), ("text", None), ("tool-call", "a"), ("tool-call", "b")]),
        ("request", [("tool-return", "a"), ("tool-return", "b")]),
        ("response", [("text", None)]),
    ]


def test_unanswered_call_gets_a_synthesized_interrupted_return():
    """A CLI turn interrupted mid-tool must not persist a dangling call —
    the invariant every provider enforces on the next request."""
    ledger = [
        {"kind": "part", "index": 0},
        {"kind": "call", "id": "t1", "name": "bash", "args": {"command": "sleep 99"}},
    ]
    out = expand_cli_activity([_resp([TextPart(content="Running.")], ledger)])
    assert _shape(out) == [
        ("response", [("text", None), ("tool-call", "t1")]),
        ("request", [("tool-return", "t1")]),
    ]
    ret = out[1].parts[0]
    assert isinstance(ret, ToolReturnPart)
    assert ret.content == MISSING_RESULT_NOTE and ret.outcome == "interrupted"


def test_prose_after_an_unanswered_call_starts_a_fresh_response():
    ledger = [
        {"kind": "call", "id": "t1", "name": "bash", "args": {}},
        {"kind": "part", "index": 0},
    ]
    out = expand_cli_activity([_resp([TextPart(content="After.")], ledger)])
    assert _shape(out) == [
        ("response", [("tool-call", "t1")]),
        ("request", [("tool-return", "t1")]),
        ("response", [("text", None)]),
    ]


def test_orphan_results_and_empty_text_parts_are_dropped():
    ledger = [
        {"kind": "part", "index": 0},
        {"kind": "result", "id": "ghost", "content": "?", "outcome": "success"},
        {"kind": "call", "id": "t1", "name": "bash", "args": {}},
        {"kind": "result", "id": "t1", "content": "ok", "outcome": "success"},
        {"kind": "result", "id": "t1", "content": "again", "outcome": "success"},
        {"kind": "part", "index": 1},
    ]
    src = _resp([TextPart(content="A"), TextPart(content="")], ledger)
    out = expand_cli_activity([src])
    assert _shape(out) == [
        ("response", [("text", None), ("tool-call", "t1")]),
        ("request", [("tool-return", "t1")]),
    ]
    assert out[1].parts[0].content == "ok"


def test_usage_and_provider_details_ride_the_last_split_only():
    ledger = [
        {"kind": "part", "index": 0},
        {"kind": "call", "id": "t1", "name": "bash", "args": {}},
        {"kind": "result", "id": "t1", "content": "ok", "outcome": "success"},
        {"kind": "part", "index": 1},
    ]
    src = _resp([TextPart(content="A"), TextPart(content="B")], ledger, provider_response_id="r1")
    out = expand_cli_activity([src])
    first, _, last = out
    assert isinstance(first, ModelResponse) and isinstance(last, ModelResponse)
    assert first.usage == RequestUsage() and last.usage == src.usage
    # The ledger key is gone from every split, other details survive.
    assert first.provider_details == {"other": 1} and last.provider_details == {"other": 1}
    assert first.model_name == last.model_name == "sonnet"
    assert first.provider_response_id == last.provider_response_id == "r1"
    assert first.timestamp == last.timestamp == src.timestamp == out[1].timestamp


def test_expansion_is_idempotent_and_leaves_ledgerless_history_alone():
    ledger = [
        {"kind": "part", "index": 0},
        {"kind": "call", "id": "t1", "name": "bash", "args": {}},
        {"kind": "result", "id": "t1", "content": "ok", "outcome": "success"},
    ]
    plain = [
        ModelRequest(parts=[UserPromptPart(content="hi")]),
        ModelResponse(parts=[TextPart(content="ok")], provider_details={"other": 1}),
    ]
    assert expand_cli_activity(plain) == plain
    assert all(a is b for a, b in zip(expand_cli_activity(plain), plain, strict=True))
    once = expand_cli_activity([*plain, _resp([TextPart(content="A")], ledger)])
    assert expand_cli_activity(once) == once


def test_empty_or_malformed_ledger_strips_the_key_and_keeps_the_turn():
    kept = expand_cli_activity([_resp([TextPart(content="x")], [])])
    assert _shape(kept) == [("response", [("text", None)])]
    assert kept[0].provider_details == {"other": 1} and kept[0].usage.input_tokens == 5
    # Not a list -> not a ledger: the message passes through untouched.
    odd = ModelResponse(parts=[TextPart(content="x")], provider_details={CLI_ACTIVITY_KEY: "no"})
    assert expand_cli_activity([odd])[0] is odd
    # A response with no parts and only an unanswered call still pairs up.
    only_call = expand_cli_activity([_resp([], [{"kind": "call", "id": "c", "name": "bash"}])])
    assert _shape(only_call) == [
        ("response", [("tool-call", "c")]),
        ("request", [("tool-return", "c")]),
    ]
    assert only_call[0].parts[0].args is None


# --- controller integration --------------------------------------------------------


def _make_tc(model, tmp_path) -> TurnController:
    deps = _make_deps(tmp_path)
    collabs = build_collaborators(
        model,
        BuiltinToolProvider(),
        deps,
        "You are a coding agent.",
        HarnessConfig(),
        get_model=lambda: model,
    )
    return TurnController(
        agent=collabs.agent,
        session=collabs.session,
        checkpoints=collabs.checkpoints,
        hooks=collabs.hooks,
        mcp=collabs.mcp,
        deps=deps,
        get_model=lambda: model,
    )


_LEDGER = [
    {"kind": "part", "index": 0},
    {"kind": "call", "id": "t1", "name": "read_file", "args": {"path": "a.py"}},
    {"kind": "result", "id": "t1", "content": "print(1)", "outcome": "success"},
    {"kind": "part", "index": 1},
]


@pytest.mark.anyio
async def test_run_turn_persists_the_expanded_tool_messages(tmp_path):
    """The success persist expands the ledger, so the session history (what
    GET history serves and a resumed TUI replays) carries the CLI's tool
    call and return as real parts, keyed off the ledger and nothing else."""

    def fn(messages, info):
        return ModelResponse(
            parts=[TextPart(content="Looking."), TextPart(content="Done.")],
            provider_details={CLI_ACTIVITY_KEY: list(_LEDGER)},
        )

    tc = _make_tc(FunctionModel(fn), tmp_path)
    outcome = await tc.run_turn("read a.py")
    assert outcome.result == "Looking.Done."
    assert _shape(tc.session.history) == [
        ("request", [("user-prompt", None)]),
        ("response", [("text", None), ("tool-call", "t1")]),
        ("request", [("tool-return", "t1")]),
        ("response", [("text", None)]),
    ]
    assert all(
        not (m.kind == "response" and m.provider_details and CLI_ACTIVITY_KEY in m.provider_details)
        for m in tc.session.history
    )
    # The next turn re-feeds that history; expansion must not double it.
    await tc.run_turn("again")
    kinds = [m.kind for m in tc.session.history]
    assert kinds.count("response") == 4 and len(tc.session.history) == 8


@pytest.mark.anyio
async def test_flush_resumable_expands_the_captured_partial_turn(tmp_path):
    """An aborted CLI turn flushes whatever the graph captured; the ledger
    attached before the abort (see ActivityLedger) is expanded on that path
    too, with the interrupted call answered."""
    tc = _make_tc(
        FunctionModel(lambda m, i: ModelResponse(parts=[TextPart(content="x")])), tmp_path
    )
    captured = [
        ModelRequest(parts=[UserPromptPart(content="go")]),
        ModelResponse(
            parts=[TextPart(content="Running.")],
            provider_details={CLI_ACTIVITY_KEY: list(_LEDGER[:2])},
        ),
    ]
    await tc._flush_resumable(captured, [])
    assert _shape(tc.session.history) == [
        ("request", [("user-prompt", None)]),
        ("response", [("text", None), ("tool-call", "t1")]),
        ("request", [("tool-return", "t1")]),
    ]
    assert tc.session.history[-1].parts[0].content == MISSING_RESULT_NOTE
