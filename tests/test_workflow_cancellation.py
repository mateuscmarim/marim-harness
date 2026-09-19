"""Abort workflows through the real controller, runner, and persistence boundaries."""

import asyncio
import os
from collections import Counter

import anyio
import pytest
from pydantic_ai.messages import ModelResponse, TextPart, ToolCallPart, ToolReturnPart
from pydantic_ai.models.function import FunctionModel

from marim_harness.claude.process import ClaudeProcess
from marim_harness.runtime.builder import HarnessBuilder
from marim_harness.session import SessionStore, TranscriptStore
from marim_harness.subagents.cli_backend import ClaudeCliRunner
from marim_harness.workflows.catalog import WorkflowBinding
from marim_harness.workflows.invocation import WorkflowInvocation, current_workflow
from marim_harness.workspace.agents import AgentDef
from tests.conftest import _make_deps
from tests.fakes import fake_claude_bin, read_claude_argvs

pytestmark = pytest.mark.anyio


def _role(backend="native"):
    return AgentDef(
        "worker-role", "Test worker", "Do the task", frozenset(), "test", backend=backend
    )


def _model(code, worker=None):
    async def respond(messages, info):
        if any(tool.name == "run_workflow" for tool in info.function_tools):
            if any(isinstance(part, ToolReturnPart) for part in messages[-1].parts):
                return ModelResponse(parts=[TextPart("done")])
            return ModelResponse(parts=[ToolCallPart("run_workflow", {"code": code}, "workflow")])
        assert worker is not None, "CLI-backed worker unexpectedly called the native model"
        task = messages[0].parts[-1].content
        return await worker(str(task))

    return FunctionModel(respond)


def _harness(tmp_path, model, *, backend="native", store=None, **config):
    store = store or SessionStore(
        path=tmp_path / "sessions" / "workflow.json",
        workspace_root=tmp_path,
        session_id="workflow-test",
        name="workflow test",
    )
    return (
        HarnessBuilder(workspace=tmp_path, model=model)
        .with_defaults()
        .with_lsp(enabled=False)
        .with_deps(_make_deps(tmp_path))
        .with_subagent(_role(backend))
        .with_config_overrides(
            store=store,
            workflow_bindings=(WorkflowBinding("worker", "worker-role"),),
            **config,
        )
        .build()
    )


def _paired(messages):
    calls = Counter(
        part.tool_call_id
        for message in messages
        for part in message.parts
        if isinstance(part, ToolCallPart)
    )
    returns = Counter(
        part.tool_call_id
        for message in messages
        for part in message.parts
        if isinstance(part, ToolReturnPart)
    )
    return calls == returns and calls["workflow"] == 1


def _assert_paired(messages):
    assert _paired(messages), messages


async def _assert_persisted_paired(store):
    """Assert the abort flushed a paired, resumable history to ``store``.

    ``_flush_resumable`` persists under a 0.25s deadline and *abandons* its
    worker thread on timeout (Ctrl-C must stay snappy); the orphan still
    lands the write, just later. Reading the file the instant the cancelled
    turn returns therefore races that orphan on a loaded runner — it showed
    up as an empty history on CI — so poll for the flushed state with a
    bounded deadline rather than asserting on the first read.
    """
    deadline = asyncio.get_running_loop().time() + 5
    while True:
        messages = store.load()[0]
        if _paired(messages) or asyncio.get_running_loop().time() >= deadline:
            _assert_paired(messages)
            return
        await asyncio.sleep(0.05)


async def _cancel(task):
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(task, 5)


@pytest.fixture
def claude_processes(monkeypatch):
    processes = []
    original_start = ClaudeProcess.start

    async def start(process):
        await original_start(process)
        processes.append((process, process.pid))

    monkeypatch.setattr(ClaudeProcess, "start", start)
    return processes


async def test_native_request_cancellation_does_not_start_queued_worker(tmp_path):
    entered = asyncio.Event()
    queued = asyncio.Event()
    cleaned = asyncio.Event()
    requests = []
    cards = []

    async def worker(task):
        requests.append(task)
        entered.set()
        try:
            await asyncio.Event().wait()
        finally:
            cleaned.set()

    code = 'import asyncio\nawait asyncio.gather(worker(task="first"), worker(task="queued"))'
    h = _harness(tmp_path, _model(code, worker), subagent_concurrency=1)

    async def announce(stream, role, task, parent):
        if task == "queued":
            queued.set()

    h.deps.ui.on_workflow_spawn = announce
    h.deps.ui.on_workflow_spawn_done = lambda *args: cards.append(args)
    running = asyncio.create_task(h.run_turn("run workflow"))

    async def wait_for_admission():
        while h.subagents._limiter.waiting_count != 1:
            await asyncio.sleep(0)

    try:
        await asyncio.wait_for(entered.wait(), 3)
        await asyncio.wait_for(queued.wait(), 3)
        await asyncio.wait_for(wait_for_admission(), 3)
        # The request boundary does not promise FIFO across independent graphs.
        # Whichever worker got capacity first is the only one allowed to start.
        started_requests = list(requests)
        assert len(started_requests) == 1
        await _cancel(running)
        assert cleaned.is_set()
        assert requests == started_requests
        assert {card[0] for card in cards} == {"workflow::wf1", "workflow::wf2"}
        await _assert_persisted_paired(h.session.store)
    finally:
        if not running.done():
            await _cancel(running)
        await h.aclose()


async def test_aborted_workflow_cannot_admit_queued_request_before_graph_cancellation(tmp_path):
    entered = asyncio.Event()
    release = asyncio.Event()
    requests = []

    async def worker(task):
        requests.append(task)
        entered.set()
        await release.wait()
        return ModelResponse(parts=[TextPart("done")])

    h = _harness(tmp_path, _model("", worker), subagent_concurrency=1)
    tasks = [asyncio.create_task(h.subagents.run("worker-role", "holder", "holder"))]
    state = WorkflowInvocation("aborted-workflow", h.deps)
    token = current_workflow.set(state)

    async def wait_for_admission():
        while h.subagents._limiter.waiting_count != 1:
            await asyncio.sleep(0)

    try:
        await asyncio.wait_for(entered.wait(), 3)
        tasks.append(asyncio.create_task(h.subagents.run("worker-role", "queued", "queued")))
        await asyncio.wait_for(wait_for_admission(), 3)
        # Reproduce the window before upstream propagates cancellation to the
        # queued graph, without relying on a particular event-loop ordering.
        state.aborted = True
        release.set()
        assert await asyncio.wait_for(tasks[0], 3) == "done"
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(tasks[1], 3)
        assert requests == ["holder"]
        assert h.subagents._limiter.running_count == 0
        assert h.subagents._limiter.waiting_count == 0
    finally:
        current_workflow.reset(token)
        release.set()
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        await h.aclose()


@pytest.mark.parametrize("repeat", [False, True])
async def test_repeated_interrupt_waits_for_native_cleanup_and_resume_never_replays(
    tmp_path, repeat
):
    entered = asyncio.Event()
    cleaning = asyncio.Event()
    release = asyncio.Event()
    cleaned = asyncio.Event()
    completed_work = tmp_path / "completed-work.txt"
    requests = []

    async def worker(task):
        requests.append(task)
        if "complete-once" in task:
            completed_work.write_text(
                completed_work.read_text() + "done\n" if completed_work.exists() else "done\n"
            )
            return ModelResponse(parts=[TextPart("completed")])
        entered.set()
        try:
            await asyncio.Event().wait()
        finally:
            cleaning.set()
            # Native requests live inside the upstream graph's AnyIO scope.
            # Resource cleanup shields level cancellation; repeated raw
            # Task.cancel() must still be kept out by the workflow owner.
            with anyio.CancelScope(shield=True):
                await release.wait()
                cleaned.set()

    code = 'await worker(task="complete-once")\nawait worker(task="block")'
    h = _harness(tmp_path, _model(code, worker))
    running = asyncio.create_task(h.run_turn("run workflow"))
    try:
        await asyncio.wait_for(entered.wait(), 3)
        running.cancel()
        await asyncio.wait_for(cleaning.wait(), 3)
        if repeat:
            running.cancel()
        await asyncio.sleep(0)
        assert not running.done(), "The turn released ownership before worker cleanup"
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(running, 5)
        assert cleaned.is_set()
        store = h.session.store
        await _assert_persisted_paired(store)
        assert completed_work.read_text() == "done\n"
        transcript = TranscriptStore(store.path, store.session_id)
        assert transcript.read_meta("workflow::wf1")["status"] == "finished"
        assert transcript.read("workflow::wf2") is not None
        await h.aclose()

        seen = []

        def resume_model(messages, info):
            _assert_paired(messages)
            seen.append(messages)
            return ModelResponse(parts=[TextPart("resumed without replay")])

        resumed = _harness(tmp_path, FunctionModel(resume_model), store=store)
        try:
            resumed.resume()
            outcome = await resumed.run_turn("continue from the interrupted workflow")
            assert outcome.result == "resumed without replay"
            assert len(seen) == 1
            assert len(requests) == 2
            assert completed_work.read_text() == "done\n"
        finally:
            await resumed.aclose()
    finally:
        release.set()
        if not running.done():
            await _cancel(running)
        await h.aclose()


async def test_claude_workflow_abort_terminates_process_and_saves_partial_transcript(
    tmp_path, monkeypatch, claude_processes
):
    binary = fake_claude_bin(tmp_path, {"turns": [[{"text": "partial"}, {"sleep": 30}]]})
    monkeypatch.setenv("MARIM_CLAUDE_CLI_BIN", binary)
    h = _harness(tmp_path, _model('await worker(task="block")'), backend="claude-cli")
    streamed = asyncio.Event()

    async def on_event(stream, event, usage):
        if type(event).__name__ in ("PartDeltaEvent", "PartStartEvent"):
            streamed.set()

    h.deps.ui.on_subagent_event = on_event
    running = asyncio.create_task(h.run_turn("run workflow"))
    try:
        await asyncio.wait_for(streamed.wait(), 3)
        await _cancel(running)
        assert len(claude_processes) == 1
        process, pid = claude_processes[0]
        assert not process.alive
        with pytest.raises(ProcessLookupError):
            os.kill(pid, 0)
        store = h.session.store
        await _assert_persisted_paired(store)
        transcript = TranscriptStore(store.path, store.session_id)
        messages = transcript.read("workflow::wf1")
        assert any("partial" in str(part) for message in messages for part in message.parts)
        assert transcript.read_meta("workflow::wf1")["cli_session_id"]
        assert len(read_claude_argvs(tmp_path)) == 1
    finally:
        if not running.done():
            await _cancel(running)
        await h.aclose()


async def test_claude_checkpoint_failure_preserves_cancellation_and_process_reaping(
    tmp_path, claude_processes, caplog
):
    binary = fake_claude_bin(tmp_path, {"turns": [[{"text": "partial"}, {"sleep": 30}]]})
    streamed = asyncio.Event()
    checkpoints = []

    async def on_event(stream, event, usage):
        streamed.set()

    def checkpoint(messages, session_id):
        checkpoints.append((messages, session_id))
        raise OSError("test checkpoint failure")

    runner = ClaudeCliRunner(on_event, None)
    running = asyncio.create_task(
        runner.run(
            binary=binary,
            prompt="block",
            system_prompt="test",
            cwd=str(tmp_path),
            allowed_tools=[],
            model=None,
            stream_id="child",
            checkpoint=checkpoint,
        )
    )
    try:
        await asyncio.wait_for(streamed.wait(), 3)
        await _cancel(running)
        assert checkpoints and checkpoints[-1][1] == "S1"
        assert "Claude spawn final checkpoint failed" in caplog.text
        assert len(claude_processes) == 1
        process, pid = claude_processes[0]
        assert not process.alive
        with pytest.raises(ProcessLookupError):
            os.kill(pid, 0)
    finally:
        if not running.done():
            await _cancel(running)
