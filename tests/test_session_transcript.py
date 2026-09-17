"""Proofs for the approved preserve-session-transcript TLC plan."""

import asyncio
import json
import threading
from copy import deepcopy
from types import SimpleNamespace

import httpx
import pytest
from pydantic_ai.messages import (
    ModelRequest,
    ModelResponse,
    TextPart,
    ToolCallPart,
    ToolReturnPart,
    UserPromptPart,
)
from pydantic_ai.models.function import FunctionModel
from pydantic_ai.models.test import TestModel
from pydantic_ai.usage import RequestUsage, RunUsage
from pydantic_ai_harness.compaction import SummarizingCompaction

from marim_harness.compaction import last_request_input_tokens
from marim_harness.session.ctrl import SessionController
from marim_harness.session.store import SessionManager
from tests.conftest import _make_deps


def _controller(tmp_path, **kwargs):
    manager = SessionManager(tmp_path / "ws", base_dir=tmp_path / "data")
    return SessionController(
        manager.create(),
        manager,
        _make_deps(tmp_path),
        max_context_tokens=150_000,
        keep_last_messages=2,
        auxiliary_model=TestModel(),
        **kwargs,
    )


def _turn(label):
    return [ModelRequest(parts=[UserPromptPart(label)]), ModelResponse(parts=[TextPart(label)])]


@pytest.mark.parametrize("provider", ["claude-cli", "codex-cli"])
@pytest.mark.parametrize("used", [119_343, 0])
def test_cli_current_context(provider, used):
    response = ModelResponse(
        parts=[TextPart("done")],
        provider_name=provider,
        usage=RequestUsage(input_tokens=2_642_890),
        provider_details={"cli_context": {"used": used, "window": 258_400}},
    )
    assert last_request_input_tokens([response]) == used
    assert response.usage.input_tokens == 2_642_890


@pytest.mark.parametrize("provider", ["claude-cli", "codex-cli"])
@pytest.mark.parametrize("report", [None, {}, {"used": -1}, {"used": True}, {"used": "12"}])
def test_cli_unknown_context(provider, report):
    older = ModelResponse(parts=[], usage=RequestUsage(input_tokens=100))
    newest = ModelResponse(
        parts=[],
        provider_name=provider,
        usage=RequestUsage(input_tokens=2_642_890),
        provider_details={"cli_context": report},
    )
    assert last_request_input_tokens([older, newest]) is None


def test_native_context():
    older = ModelResponse(parts=[], usage=RequestUsage(input_tokens=100))
    newest = ModelResponse(parts=[], usage=RequestUsage(input_tokens=234))
    assert last_request_input_tokens([older, newest]) == 234
    assert last_request_input_tokens([older, ModelResponse(parts=[])]) == 100
    assert last_request_input_tokens([]) is None


@pytest.mark.anyio
async def test_cli_gate(tmp_path, monkeypatch):
    controller = _controller(tmp_path)
    controller.history = [
        ModelResponse(
            parts=[TextPart("done")],
            provider_name="codex-cli",
            usage=RequestUsage(input_tokens=2_642_890),
            provider_details={"cli_context": {"used": 119_343, "window": 258_400}},
        )
    ]
    controller.last_input_tokens = last_request_input_tokens(controller.history)
    calls = []

    async def reduce(**kwargs):
        calls.append(kwargs)
        return True

    monkeypatch.setattr(controller, "_reduce_and_commit", reduce)
    assert await controller.maybe_compact() is False
    assert calls == []


def test_cli_ledger(tmp_path):
    controller = _controller(tmp_path)
    recorded = []

    class Recorder:
        def record(self, usage, **kwargs):
            recorded.append(usage.input_tokens)

    controller.stats_recorder = Recorder()
    controller.add_usage(RunUsage(input_tokens=2_642_890))
    assert recorded == [2_642_890]
    assert controller.usage.input_tokens == 2_642_890


def _conversation():
    return [
        ModelRequest(parts=[UserPromptPart("original question")]),
        ModelResponse(parts=[ToolCallPart("read_file", {"path": "a"}, "tc-1")]),
        ModelRequest(parts=[ToolReturnPart("read_file", "original output " * 1000, "tc-1")]),
        ModelResponse(parts=[TextPart("original answer")]),
        *_turn("second"),
        *_turn("third"),
    ]


async def _reduce(controller, stage="summary"):
    controller.max_context_tokens = 1000
    controller.mask_observations = stage == "clear"
    controller.mask_keep_recent = 0
    if stage == "summary":
        controller.compaction_strategy = SummarizingCompaction(
            max_tokens=1,
            keep_messages=2,
            model=FunctionModel(lambda messages, info: ModelResponse(parts=[TextPart("summary")])),
        )
    else:
        controller.compaction_strategy = None
    assert await controller.maybe_compact(force=stage != "clear")


@pytest.mark.anyio
@pytest.mark.parametrize("stage", ["summary", "trim", "clear"])
async def test_reduction_preserves_transcript(tmp_path, stage):
    controller = _controller(tmp_path)
    original = _conversation()
    expected = deepcopy(original)
    controller.history = original
    controller.persist()
    await _reduce(controller, stage)
    assert controller.history != expected
    assert controller.transcript == expected
    # A later context-only mutation cannot alter the archived recorded content.
    controller.history[-1].parts[0].content = "mutated context"
    assert controller.transcript == expected


@pytest.mark.anyio
async def test_post_reduction_turn(tmp_path):
    from marim_harness.runtime.harness import Harness
    from marim_harness.tools.provider import BuiltinToolProvider

    received = []

    def answer(messages, info):
        received.extend(deepcopy(messages))
        return ModelResponse(parts=[TextPart("new answer")])

    controller = _controller(tmp_path)
    harness = Harness(
        FunctionModel(answer),
        BuiltinToolProvider(),
        controller.deps,
        instructions="test",
        store=controller.store,
        manager=controller.manager,
    )
    controller = harness.session
    original = _conversation()
    controller.history = deepcopy(original)
    controller.auxiliary_model = TestModel()
    await _reduce(controller)
    reduced = deepcopy(list(controller.history))
    controller.max_context_tokens = 150_000
    await harness.run_turn("new question")
    # Pydantic merges adjacent requests and adds run IDs before the provider;
    # compare every actual input part, not those transport envelope mutations.
    reduced_parts = [part for message in reduced for part in message.parts]
    received_parts = [part for message in received for part in message.parts]
    assert received_parts[:-1] == reduced_parts
    assert received_parts[-1].content.endswith("new question")
    assert controller.transcript[: len(original)] == original
    assert len(controller.transcript) == len(original) + 2
    assert controller.transcript[-2].parts[-1].content.endswith("new question")
    assert controller.transcript[-1].parts[0].content == "new answer"


@pytest.mark.anyio
async def test_repeated_reductions(tmp_path):
    controller = _controller(tmp_path)
    expected = _conversation()
    controller.history = deepcopy(expected)
    for label in ["fourth", "fifth", "sixth"]:
        await _reduce(controller)
        new = _turn(label)
        controller.history.extend(new)
        expected.extend(deepcopy(new))
        controller.persist()
        assert controller.transcript == expected


@pytest.mark.anyio
async def test_transcript_resume(tmp_path):
    controller = _controller(tmp_path)
    controller.history = _conversation()
    expected = deepcopy(list(controller.history))
    await _reduce(controller)
    reduced = list(controller.history)
    saved_id = controller.store.session_id
    controller = _controller(tmp_path)
    controller.store = controller.manager.store(saved_id)
    controller.resume()
    assert controller.transcript == expected
    assert controller.history == reduced
    controller.history.extend(_turn("after resume"))
    controller.persist()
    payload = json.loads(controller.store.path.read_text())
    assert len(payload["transcript"]) == 10
    assert payload["transcript"][-1]["parts"][0]["content"] == "after resume"


def test_legacy_transcript(tmp_path):
    controller = _controller(tmp_path)
    original = _turn("available old content")
    controller.store.save(original, RunUsage())
    assert "transcript" not in json.loads(controller.store.path.read_text())
    controller.resume()
    assert controller.transcript == original


@pytest.mark.parametrize(
    "start,stop,expected",
    [(0, None, ["a", "b", "c"]), (1, 2, ["b"]), (2, None, ["c"]), (0, 0, []), (5, None, [])],
)
def test_slice_message_parts(start, stop, expected):
    from marim_harness.session.history import slice_message_parts

    messages = [
        ModelRequest(parts=[UserPromptPart("a"), UserPromptPart("b")]),
        ModelResponse(parts=[TextPart("c")]),
    ]
    before = deepcopy(messages)
    result = slice_message_parts(messages, start=start, stop=stop)
    assert [part.content for message in result for part in message.parts] == expected
    assert messages == before


@pytest.mark.anyio
@pytest.mark.parametrize("operation", ["reset", "new_session"])
async def test_transcript_reset(tmp_path, operation):
    controller = _controller(tmp_path)
    controller.history = _conversation()
    await _reduce(controller)
    getattr(controller, operation)()
    assert controller.history == []
    assert controller.transcript == []
    controller.persist()
    assert json.loads(controller.store.path.read_text())["transcript"] == []


@pytest.mark.anyio
async def test_transcript_switch(tmp_path):
    controller = _controller(tmp_path)
    original = _conversation()
    controller.history = deepcopy(original)
    await _reduce(controller)
    first = controller.store.session_id
    target = controller.manager.create("target")
    target_messages = _turn("target")
    target.save(target_messages, RunUsage())
    controller.switch_session(target.session_id)
    assert controller.transcript == target_messages
    controller.switch_session(first)
    assert controller.transcript == original


@pytest.mark.anyio
@pytest.mark.parametrize("damage", ["json", "tasks"])
async def test_transcript_switch_failure(tmp_path, damage):
    from marim_harness.session.store import SessionLoadError

    controller = _controller(tmp_path)
    controller.history = _conversation()
    await _reduce(controller)
    before = deepcopy(list(controller.history)), deepcopy(controller.transcript)
    active = controller.store
    target = controller.manager.create("broken")
    target.save(_turn("target"), RunUsage())
    payload = json.loads(target.path.read_text())
    payload["tasks"] = 123
    target.path.write_text("{" if damage == "json" else json.dumps(payload))
    with pytest.raises(SessionLoadError):
        controller.switch_session(target.session_id)
    assert controller.store is active
    assert (controller.history, controller.transcript) == before


@pytest.mark.anyio
async def test_transcript_atomic_failure(tmp_path, monkeypatch):
    from marim_harness.session import store as store_module

    controller = _controller(tmp_path)
    controller.history = _conversation()
    controller.persist()
    before = controller.store.path.read_bytes()

    def fail(*args, **kwargs):
        raise OSError("injected before replacement")

    monkeypatch.setattr(store_module, "atomic_write_text", fail)
    with pytest.raises(OSError, match="injected"):
        await _reduce(controller)
    assert controller.store.path.read_bytes() == before


@pytest.mark.anyio
@pytest.mark.parametrize("stage", ["summary", "clear", "legacy"])
async def test_checkpoint_transcript(tmp_path, stage):
    from marim_harness.session.checkpoints import CheckpointManager

    controller = _controller(tmp_path)
    original = _conversation()
    controller.history = deepcopy(original)
    checkpoints = CheckpointManager(controller)
    controller.on_history_restructured = checkpoints.invalidate_after_compaction
    if stage == "summary":
        await _reduce(controller, stage)
    index = checkpoints.snapshot("next")
    if stage == "legacy":
        path = checkpoints._sidecar_path()
        payload = json.loads(path.read_text())
        payload["checkpoints"][0].pop("transcript_len", None)
        path.write_text(json.dumps(payload))
    controller.history.extend(_turn("after checkpoint"))
    if stage == "clear":
        await _reduce(controller, stage)
    controller.persist()
    before = deepcopy(controller.transcript)
    # Reload the sidecar so the test covers the on-disk boundary, not only memory.
    checkpoints.reload()
    checkpoints.rewind(index)
    assert controller.transcript == original
    assert len(json.loads(controller.store.path.read_text())["transcript"]) == len(original)
    assert checkpoints.undo_rewind()
    assert controller.transcript == before
    assert len(json.loads(controller.store.path.read_text())["transcript"]) == len(before)


@pytest.mark.anyio
async def test_transcript_delayed_save(tmp_path, monkeypatch):
    controller = _controller(tmp_path)
    original = _conversation()
    controller.history = deepcopy(original)
    await _reduce(controller)
    entered, release = threading.Event(), threading.Event()
    snapshots = []
    save = controller.store.save

    def delayed(history, *args, **kwargs):
        if not entered.is_set():
            entered.set()
            assert release.wait(5)
        snapshots.append((deepcopy(history.context), deepcopy(history.transcript)))
        save(history, *args, **kwargs)

    monkeypatch.setattr(controller.store, "save", delayed)
    first = asyncio.create_task(asyncio.to_thread(controller.persist, force=True))
    assert await asyncio.to_thread(entered.wait, 5)
    new = _turn("newer generation")
    controller.history.extend(new)
    second = asyncio.create_task(asyncio.to_thread(controller.persist))
    release.set()
    await asyncio.gather(first, second)
    controller.persist()
    assert len(snapshots) == 2
    assert snapshots[0][1] == original
    assert snapshots[1][1] == original + new
    data = json.loads(controller.store.path.read_text())
    assert len(data["transcript"]) == len(original) + 2
    assert data["messages"][-1]["parts"][0]["content"] == "newer generation"
    assert data["transcript"][-1]["parts"][0]["content"] == "newer generation"


@pytest.mark.anyio
@pytest.mark.parametrize("finish", ["cancel", "approve"])
async def test_transcript_approval_cancel(tmp_path, finish):
    from marim_harness.runtime.harness import Harness
    from marim_harness.runtime.permissions import Mode
    from marim_harness.tools.provider import BuiltinToolProvider

    controller = _controller(tmp_path)
    original = _conversation()
    seen = []

    async def approval(call):
        payload = json.loads(controller.store.path.read_text())
        seen.append(payload["transcript"])
        assert len(payload["transcript"]) == len(original)
        if finish == "cancel":
            raise asyncio.CancelledError
        return True

    calls = 0

    def model(messages, info):
        nonlocal calls
        calls += 1
        if calls == 1:
            return ModelResponse(
                parts=[ToolCallPart("write_file", {"path": "new.txt", "content": "new"}, "write-1")]
            )
        return ModelResponse(parts=[TextPart("done")])

    controller.deps.workspace.mode = Mode.ask
    controller.deps.ui.request_approval = approval
    harness = Harness(
        FunctionModel(model),
        BuiltinToolProvider(),
        controller.deps,
        instructions="test",
        store=controller.store,
    )
    controller = harness.session
    controller.history = deepcopy(original)
    controller.auxiliary_model = TestModel()
    await _reduce(controller)
    if finish == "cancel":
        with pytest.raises(asyncio.CancelledError):
            await harness.run_turn("write new file")
        assert controller.transcript == original
    else:
        await harness.run_turn("write new file")
        assert len(controller.transcript) == len(original) + 4
    assert len(seen) == 1
    assert len(json.loads(controller.store.path.read_text())["transcript"]) == len(
        controller.transcript
    )


def _history_app(controller, tmp_path, monkeypatch):
    from marim_harness.server import http
    from marim_harness.server.supervisor import SessionSupervisor
    from marim_harness.server.workspaces import WorkspaceRegistry

    registry = WorkspaceRegistry(tmp_path / "registry.json", tmp_path / "managed")
    record = registry.register("test", tmp_path)
    manager = SimpleNamespace(
        session_path=lambda sid: controller.store.path.with_name(sid + ".json")
    )
    monkeypatch.setattr(http, "SessionManager", lambda root: manager)
    app = http.create_app(registry=registry, supervisor=SessionSupervisor(), token="test")
    base = f"/v1/workspaces/{record.id}/sessions/{controller.store.session_id}"
    return app, base, record.id


@pytest.mark.anyio
async def test_http_transcript_pages(tmp_path, monkeypatch):
    from pydantic_ai.messages import ModelMessagesTypeAdapter
    from pydantic_ai_harness.compaction import estimate_token_count

    controller = _controller(tmp_path)
    original = _conversation()
    controller.history = deepcopy(original)
    await _reduce(controller)
    app, base, _ = _history_app(controller, tmp_path, monkeypatch)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app), base_url="http://test"
    ) as client:
        headers = {"Authorization": "Bearer test"}
        pages = []
        for offset in range(0, len(original), 3):
            response = await client.get(
                base + "/history", headers=headers, params={"offset": offset, "limit": 3}
            )
            assert response.status_code == 200
            payload = response.json()
            assert payload["message_count"] == len(original)
            assert payload["history_seq"] == 0
            assert payload["context_tokens"] == estimate_token_count(controller.history)
            pages.extend(payload["messages"])
        assert ModelMessagesTypeAdapter.validate_python(pages) == original
        assert (await client.get(base + "/history")).status_code == 401
        assert (await client.get(base + "/history?offset=bad", headers=headers)).status_code == 400
        assert (await client.get(base + "missing/history", headers=headers)).status_code == 404


@pytest.mark.anyio
@pytest.mark.parametrize(
    "damaged", [None, {}, "bad", [1], [{"kind": "bad"}], [{"kind": "request", "parts": 42}]]
)
async def test_malformed_transcript(tmp_path, monkeypatch, damaged):
    from marim_harness.session.store import SessionLoadError

    controller = _controller(tmp_path)
    controller.history = _conversation()
    await _reduce(controller)
    data = json.loads(controller.store.path.read_text())
    data["transcript"] = damaged
    controller.store.path.write_text(json.dumps(data))
    with pytest.raises(SessionLoadError):
        controller.resume()
    app, base, _ = _history_app(controller, tmp_path, monkeypatch)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app), base_url="http://test"
    ) as client:
        response = await client.get(base + "/history", headers={"Authorization": "Bearer test"})
        assert response.status_code == 500
        assert response.json()["error"]["code"] == "unreadable"


@pytest.mark.anyio
async def test_tui_transcript(tmp_path, monkeypatch):
    from pydantic_ai_harness.compaction import estimate_token_count

    from marim_harness.interfaces.tui.app import HarnessApp
    from marim_harness.interfaces.tui.link import LocalSessionLink
    from marim_harness.runtime.harness import Harness
    from marim_harness.server.attach import RemoteTarget
    from marim_harness.server.bus import EventBus
    from marim_harness.server.client import RemoteSessionHost
    from marim_harness.server.host import SessionHost
    from marim_harness.tools.provider import BuiltinToolProvider

    controller = _controller(tmp_path)
    harness = Harness(
        TestModel(call_tools=[]),
        BuiltinToolProvider(),
        controller.deps,
        instructions="test",
        store=controller.store,
    )
    controller = harness.session
    original = _conversation()
    controller.history = deepcopy(original)
    controller.auxiliary_model = TestModel()
    await _reduce(controller)
    local = LocalSessionLink(harness, SessionHost(harness, EventBus(), autonomous_wake=False))
    app, base, wid = _history_app(controller, tmp_path, monkeypatch)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app),
        base_url="http://test",
        headers={"Authorization": "Bearer test"},
    ) as client:
        remote = RemoteSessionHost(
            RemoteTarget("http://test", "test", wid, controller.store.session_id, tmp_path),
            tmp_path,
            client=client,
        )
        local_history = await local.history()
        remote_history = await remote.history()
        assert local_history.messages == remote_history.messages == original
        assert remote.info.history_tokens == estimate_token_count(controller.history)
        local_app = SimpleNamespace(harness=harness)
        remote_app = SimpleNamespace(harness=None, _remote_history=remote_history.messages)
        assert HarnessApp.history_messages.fget(local_app) == original
        assert HarnessApp.history_messages.fget(remote_app) == original
    await local.close()


@pytest.mark.anyio
@pytest.mark.parametrize("reported", [None, 0, 123])
async def test_remote_context_tokens(tmp_path, reported):
    from pydantic_ai.messages import ModelMessagesTypeAdapter
    from pydantic_ai_harness.compaction import estimate_token_count

    from marim_harness.server.attach import RemoteTarget
    from marim_harness.server.client import RemoteSessionHost

    messages = _turn("archived " * 500)
    page = {
        "messages": ModelMessagesTypeAdapter.dump_python(messages, mode="json"),
        "message_count": len(messages),
        "history_seq": 0,
    }
    if reported is not None:
        page["context_tokens"] = reported
    async with httpx.AsyncClient(
        base_url="http://test",
        transport=httpx.MockTransport(lambda request: httpx.Response(200, json=page)),
    ) as client:
        remote = RemoteSessionHost(
            RemoteTarget("http://test", "test", "ws", "id", tmp_path), tmp_path, client=client
        )
        assert (await remote.history()).messages == messages
        assert remote.info.history_tokens == (
            estimate_token_count(messages) if reported is None else reported
        )


@pytest.mark.anyio
async def test_repaired_context_does_not_hide_next_prompt(tmp_path):
    from marim_harness.runtime.harness import Harness
    from marim_harness.tools.provider import BuiltinToolProvider

    controller = _controller(tmp_path)
    original = [
        *_turn("old"),
        ModelResponse(parts=[ToolCallPart("read_file", {"path": "a"}, "dangling")]),
    ]
    controller.store.save(original, RunUsage())
    harness = Harness(
        TestModel(call_tools=[]),
        BuiltinToolProvider(),
        controller.deps,
        instructions="test",
        store=controller.store,
    )
    harness.session.resume()
    await harness.run_turn("new prompt")
    transcript = harness.session.transcript
    assert transcript[: len(original)] == original
    assert len(transcript) == len(original) + 2
    assert transcript[-2].parts[-1].content.endswith("new prompt")


@pytest.mark.anyio
async def test_cancelled_provider_preserves_prompt_after_reduction(tmp_path):
    from marim_harness.runtime.harness import Harness
    from marim_harness.tools.provider import BuiltinToolProvider

    async def cancel(messages, info):
        raise asyncio.CancelledError

    controller = _controller(tmp_path)
    harness = Harness(
        FunctionModel(cancel),
        BuiltinToolProvider(),
        controller.deps,
        instructions="test",
        store=controller.store,
    )
    original = _conversation()
    controller = harness.session
    controller.history = deepcopy(original)
    controller.auxiliary_model = TestModel()
    await _reduce(controller)
    with pytest.raises(asyncio.CancelledError):
        await harness.run_turn("cancelled prompt")
    assert controller.transcript[: len(original)] == original
    assert len(controller.transcript) == len(original) + 1
    assert controller.transcript[-1].parts[-1].content.endswith("cancelled prompt")
    controller.resume()
    assert len(controller.transcript) == len(original) + 1


def test_transcript_media_and_metadata_roundtrip(tmp_path, monkeypatch):
    from pydantic_ai.messages import BinaryContent

    from marim_harness.session.store import _header_fields

    monkeypatch.setenv("MARIM_IMAGE_CACHE_DIR", str(tmp_path / "images"))
    controller = _controller(tmp_path)
    image = BinaryContent(data=b"image-bytes", media_type="image/png")
    original = [ModelRequest(parts=[UserPromptPart(["image", image])])]
    controller.restore_history(_turn("compacted"), original)
    controller.persist()
    controller.store.name = "renamed"
    controller.store.save_meta()
    controller.store.save_jobs([])
    assert _header_fields(controller.store.path)["message_count"] == 1
    controller.resume()
    assert controller.transcript[0].parts[0].content[0] == "image"
    restored = controller.transcript[0].parts[0].content[1]
    assert restored.data == image.data
    assert restored.media_type == image.media_type


@pytest.mark.anyio
async def test_checkpoint_normalized_context(tmp_path):
    from marim_harness.runtime.harness import Harness
    from marim_harness.tools.provider import BuiltinToolProvider

    controller = _controller(tmp_path)
    harness = Harness(
        TestModel(call_tools=[]),
        BuiltinToolProvider(),
        controller.deps,
        instructions="test",
        store=controller.store,
    )
    controller = harness.session
    original = _conversation()
    controller.history = deepcopy(original)
    controller.auxiliary_model = TestModel()
    await _reduce(controller)
    before_parts = deepcopy([part for message in controller.history for part in message.parts])
    await harness.run_turn("next question")
    checkpoint = harness.checkpoints.list()[-1]
    harness.checkpoints.rewind(checkpoint.index)
    assert [part for message in controller.history for part in message.parts] == before_parts
    assert controller.transcript == original


@pytest.mark.anyio
async def test_http_media_refs_are_not_rehydrated(tmp_path, monkeypatch):
    from pydantic_ai.messages import BinaryContent

    monkeypatch.setenv("MARIM_IMAGE_CACHE_DIR", str(tmp_path / "images"))
    controller = _controller(tmp_path)
    original = [
        ModelRequest(
            parts=[
                UserPromptPart(
                    ["image", BinaryContent(data=b"saved-image", media_type="image/png")]
                )
            ]
        )
    ]
    controller.restore_history(_turn("compacted"), original)
    controller.persist()
    raw_transcript = json.loads(controller.store.path.read_text())["transcript"]
    app, base, _ = _history_app(controller, tmp_path, monkeypatch)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app), base_url="http://test"
    ) as client:
        response = await client.get(base + "/history", headers={"Authorization": "Bearer test"})
    assert response.status_code == 200
    assert response.json()["messages"] == raw_transcript


def test_paired_save_preserves_legacy_contract(tmp_path):
    from inspect import signature

    controller = _controller(tmp_path)
    store = controller.store
    assert list(signature(store.save).parameters) == [
        "history",
        "usage",
        "tasks",
        "duration_seconds",
        "jobs",
    ]
    from marim_harness.session.store import SessionMessages

    context, transcript = _turn("context"), _turn("recorded conversation")
    usage, tasks, jobs = RunUsage(input_tokens=11), [{"text": "task", "status": "pending"}], []
    store.save(context, usage, tasks, 7.0, jobs)
    legacy = json.loads(store.path.read_text())
    assert "transcript" not in legacy
    assert legacy["messages"][-1]["parts"][0]["content"] == "context"
    store.save(SessionMessages(context, transcript), usage, tasks, 7.0, jobs)
    saved = json.loads(store.path.read_text())
    assert saved["messages"] == legacy["messages"]
    assert saved["transcript"][-1]["parts"][0]["content"] == "recorded conversation"
    assert saved["tokens"] == legacy["tokens"]
    assert saved["tasks"] == tasks and saved["jobs"] == jobs
    assert saved["duration_seconds"] == 7.0
