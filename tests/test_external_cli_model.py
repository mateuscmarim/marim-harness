"""The ExternalCliModel base: one seam set shared by claude-cli and codex-cli."""

from __future__ import annotations

import pytest

from marim_harness.config.claude_cli_model import ClaudeCliModel
from marim_harness.config.external_cli import ExternalCliModel, TextFolder
from marim_harness.session.ctrl import aux_model_for
from tests.conftest import _make_deps, _make_harness, _text_model

pytestmark = pytest.mark.anyio


class _Fake(ExternalCliModel):
    provider_id = "fake-cli"

    def __init__(self) -> None:
        super().__init__()
        self.clones: list[str] = []

    def ephemeral_clone(self, *, cwd: str) -> _Fake:
        clone = _Fake()
        clone.cwd = cwd
        clone.ephemeral = True
        self.clones.append(cwd)
        return clone

    @property
    def model_name(self) -> str:
        return "fake"

    async def request(self, messages, model_settings, model_request_parameters):  # pragma: no cover
        raise NotImplementedError


class _Bare(ExternalCliModel):
    """An ExternalCliModel subclass that does NOT override `ephemeral_clone` —
    for exercising the base class's own defaults (the loud raise, the
    steer/compact_remote no-ops) rather than a subclass's override of them."""

    provider_id = "bare-cli"

    @property
    def model_name(self) -> str:
        return "bare"

    async def request(self, messages, model_settings, model_request_parameters):  # pragma: no cover
        raise NotImplementedError


class _FakePartsManager:
    """A minimal stand-in for pydantic-ai's ModelResponsePartsManager: records
    every `handle_text_delta` call so tests can assert TextFolder's exact
    vendor-part-id bookkeeping, and forwards each call through as its own
    ``event`` (TextFolder only ever forwards whatever the manager yields)."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, str]] = []

    def handle_text_delta(self, *, vendor_part_id: str, content: str):
        self.calls.append((vendor_part_id, content))
        return [(vendor_part_id, content)]


def test_claude_cli_model_is_an_external_cli_model():
    assert issubclass(ClaudeCliModel, ExternalCliModel)
    assert ClaudeCliModel.provider_id == "claude-cli"
    assert ClaudeCliModel("x").system == "claude-cli"


def test_base_defaults_are_inert():
    m = _Fake()
    assert m.system == "fake-cli"
    assert m.mode_getter is None and m.cwd == "."
    assert m.request_approval is None and m.ask_user is None
    assert m.scratchpad_getter is None and m.thinking_getter is None
    assert m.session_ref_getter is None and m.on_session_ref is None
    assert m.steer("x") is False
    assert m.ephemeral is False


def test_aux_model_for_clones_any_external_cli_model():
    raw = _Fake()
    aux = aux_model_for(raw, cwd="/ws")
    assert aux is not raw and isinstance(aux, _Fake) and aux.ephemeral and aux.cwd == "/ws"


def test_wire_cli_model_binds_all_seams(tmp_path):
    harness = _make_harness(_text_model(), _make_deps(tmp_path))
    m = _Fake()
    harness.wire_cli_model(m)
    assert m.mode_getter is not None and m.mode_getter() == harness.mode.value
    assert m.cwd == str(harness.deps.workspace.root)
    assert m.on_activity is harness.deps.ui.on_cli_activity
    assert m.job_registry is harness.deps.jobs
    assert m.on_jobs_settled is not None
    assert m.on_subagent is harness.deps.ui.on_subagent_event
    assert m.on_subagent_model is harness.deps.ui.on_subagent_model
    assert m.on_subagent_notice is harness.deps.ui.on_subagent_notice
    assert m.on_subagent_usage is harness.deps.ui.on_subagent_usage
    assert m.request_approval is harness.deps.ui.request_approval
    assert m.ask_user is harness.deps.ui.ask_user
    assert m.scratchpad_getter is not None
    assert m.thinking_getter is not None and m.thinking_getter() == harness.thinking_level_id
    assert m.session_ref_getter is not None and m.on_session_ref is not None


def test_wire_cli_model_ignores_other_models(tmp_path):
    harness = _make_harness(_text_model(), _make_deps(tmp_path))

    class _Plain:
        pass

    plain = _Plain()
    harness.wire_cli_model(plain)  # no attribute errors, nothing set
    assert not hasattr(plain, "mode_getter")


async def test_observed_job_persist_defers_dirty_history_and_ignores_rebound_store(
    tmp_path, monkeypatch
):
    from unittest.mock import Mock

    from marim_harness.session import SessionManager

    manager = SessionManager(tmp_path)
    store = manager.create("original")
    harness = _make_harness(_text_model(), _make_deps(tmp_path), store=store, manager=manager)
    model = _Fake()
    harness.wire_cli_model(model)
    save_jobs = Mock(return_value=True)
    monkeypatch.setattr(store, "save_jobs", save_jobs)
    harness.deps.approval_round_active = True
    model.on_jobs_settled()
    await harness.cli_job_persistence.flush()
    save_jobs.assert_not_called()
    harness.deps.approval_round_active = False
    model.on_jobs_settled()
    await harness.cli_job_persistence.flush()
    save_jobs.assert_called_once_with([])
    save_jobs.reset_mock()
    monkeypatch.setattr(harness.session, "store", manager.create("incoming"))
    model.on_jobs_settled()
    await harness.cli_job_persistence.flush()
    save_jobs.assert_not_called()


async def test_releasing_conversation_invalidates_old_jobs_and_rebinds_persistence(
    tmp_path, monkeypatch
):
    from unittest.mock import Mock

    from marim_harness.runtime.backend_jobs import ClaudeJobObserver
    from marim_harness.session import SessionManager

    model = _Fake()
    manager = SessionManager(tmp_path)
    harness = _make_harness(
        model, _make_deps(tmp_path), store=manager.create("original"), manager=manager
    )
    harness.wire_cli_model(model)
    old = ClaudeJobObserver(model.job_registry, model.on_jobs_settled)
    old.start("old-agent", {})
    incoming = manager.create("incoming")
    monkeypatch.setattr(harness.session, "store", incoming)
    harness._release_cli_conversation()
    old.start("late-agent", {})
    old.close()
    assert harness.deps.jobs.list() == []
    save_jobs = Mock(return_value=True)
    monkeypatch.setattr(incoming, "save_jobs", save_jobs)
    current = ClaudeJobObserver(model.job_registry, model.on_jobs_settled)
    current.start("new-agent", {})
    current.finish("new-agent", "new report", "done")
    await harness.cli_job_persistence.flush()
    save_jobs.assert_called_once_with(harness.deps.jobs.export_settled())


def test_base_ephemeral_clone_raises_when_a_subclass_forgets_to_override():
    with pytest.raises(NotImplementedError, match="_Bare must implement ephemeral_clone"):
        _Bare().ephemeral_clone(cwd="/ws")


async def test_base_compact_remote_is_a_noop():
    assert await _Bare().compact_remote() is None


def _folder(pm, *, cards: bool, fold_text=None, is_call=None, activity_events=None) -> TextFolder:
    return TextFolder(
        pm,
        (lambda events: None) if cards else None,
        activity_events=activity_events or (lambda chunk: []),
        fold_text=fold_text or (lambda chunk, first: ""),
        is_call=is_call or (lambda chunk: False),
    )


async def test_text_folder_emit_text_cards_mode_bootstraps_then_deltas():
    pm = _FakePartsManager()
    folder = _folder(pm, cards=True)
    events = [e async for e in folder.emit_text("hello")]
    # a brand-new vendor part id gets an empty-content bootstrap call BEFORE
    # the real delta, so a delta-only consumer never misses the first chunk.
    assert pm.calls == [("text-0", ""), ("text-0", "hello")]
    assert events == [("text-0", ""), ("text-0", "hello")]


async def test_text_folder_emit_text_cards_mode_skips_bootstrap_on_the_same_part():
    pm = _FakePartsManager()
    folder = _folder(pm, cards=True)
    _ = [e async for e in folder.emit_text("a")]
    _ = [e async for e in folder.emit_text("b")]
    # same part_n ("text-0") both times -> only the FIRST call bootstraps.
    assert pm.calls == [("text-0", ""), ("text-0", "a"), ("text-0", "b")]


async def test_text_folder_emit_text_fold_mode_concatenates_prose_deltas():
    pm = _FakePartsManager()
    folder = _folder(pm, cards=False)
    _ = [e async for e in folder.emit_text("fir")]
    assert folder.folded_any is True
    _ = [e async for e in folder.emit_text("st")]
    # Both chunks land on the SAME part id ("text-0") and join with NOTHING
    # between them: prose arrives a few characters per delta, so a separator
    # here would shred every sentence.
    assert pm.calls == [
        ("text-0", ""),
        ("text-0", "fir"),
        ("text-0", "st"),
    ]


async def test_text_folder_emit_text_fold_mode_blank_line_separates_a_tool_line():
    pm = _FakePartsManager()
    folder = _folder(pm, cards=False, fold_text=lambda chunk, first: "▸ Bash ls")
    _ = [e async for e in folder.emit_text("be")]
    _ = [e async for e in folder.emit_tool("cmd")]
    assert folder.after_tool is True  # armed: the next prose starts a new block
    _ = [e async for e in folder.emit_text("af")]
    _ = [e async for e in folder.emit_text("ter")]
    # Only the delta that FOLLOWS a folded ▸ line is blank-line separated; the
    # ones after it continue the same block.
    assert pm.calls == [
        ("text-0", ""),
        ("text-0", "be"),
        ("text-0", "▸ Bash ls"),
        ("text-0", "\n\naf"),
        ("text-0", "ter"),
    ]
    assert folder.after_tool is False


async def test_text_folder_emit_tool_cards_mode_pushes_activity_and_bumps_part_n_for_calls():
    pushed = []

    async def on_activity(events):
        pushed.append(events)

    pm = _FakePartsManager()
    folder = TextFolder(
        pm,
        on_activity,
        activity_events=lambda chunk: [f"event-for-{chunk}"],
        fold_text=lambda chunk, first: "",
        is_call=lambda chunk: True,
    )
    async for _ in folder.emit_tool("call-1"):
        pass  # pragma: no cover - cards mode yields nothing
    assert pushed == [["event-for-call-1"]]
    assert folder.part_n == 1  # a tool CALL opens a fresh part for following prose


async def test_text_folder_emit_tool_cards_mode_skips_callback_when_no_events():
    pushed = []

    async def on_activity(events):
        pushed.append(events)

    pm = _FakePartsManager()
    folder = TextFolder(
        pm,
        on_activity,
        activity_events=lambda chunk: [],  # nothing worth rendering for this chunk
        fold_text=lambda chunk, first: "",
        is_call=lambda chunk: True,
    )
    async for _ in folder.emit_tool("call-1"):
        pass  # pragma: no cover
    assert pushed == []  # on_activity never invoked with an empty event list
    assert folder.part_n == 1  # but a CALL still bumps part_n regardless


async def test_text_folder_emit_tool_cards_mode_result_does_not_bump_part_n():
    """A tool RESULT (not a call) still pushes its activity events, but must
    NOT open a fresh text part — only a call does, so prose continues to
    interleave right after the result on the SAME part."""
    pushed = []

    async def on_activity(events):
        pushed.append(events)

    pm = _FakePartsManager()
    folder = TextFolder(
        pm,
        on_activity,
        activity_events=lambda chunk: [f"event-for-{chunk}"],
        fold_text=lambda chunk, first: "",
        is_call=lambda chunk: False,
    )
    async for _ in folder.emit_tool("result-1"):
        pass  # pragma: no cover
    assert pushed == [["event-for-result-1"]]
    assert folder.part_n == 0


async def test_text_folder_emit_tool_fold_mode_folds_a_nonempty_segment():
    pm = _FakePartsManager()
    folder = _folder(pm, cards=False, fold_text=lambda chunk, first: "▸ ran ls")
    events = [e async for e in folder.emit_tool("cmd")]
    assert pm.calls == [("text-0", ""), ("text-0", "▸ ran ls")]
    assert events == [("text-0", ""), ("text-0", "▸ ran ls")]
    assert folder.folded_any is True


async def test_text_folder_emit_tool_fold_mode_skips_an_empty_segment():
    pm = _FakePartsManager()
    folder = _folder(pm, cards=False, fold_text=lambda chunk, first: "")
    events = [e async for e in folder.emit_tool("noise")]
    assert events == []
    assert pm.calls == []
    assert folder.folded_any is False


# --- ActivityLedger -----------------------------------------------------------------


def _call_event(call_id: str = "t1"):
    from pydantic_ai.messages import FunctionToolCallEvent, ToolCallPart

    return FunctionToolCallEvent(
        part=ToolCallPart(tool_name="read_file", args={"path": "a"}, tool_call_id=call_id)
    )


def _result_event(call_id: str = "t1", content: str = "ok", *, failed: bool = False):
    from pydantic_ai.messages import FunctionToolResultEvent, ToolReturnPart

    return FunctionToolResultEvent(
        part=ToolReturnPart(
            tool_name="tool",
            content=content,
            tool_call_id=call_id,
            outcome="failed" if failed else "success",
        )
    )


def test_activity_ledger_attaches_on_the_first_tool_entry_only():
    from pydantic_ai.messages import PartStartEvent, TextPart

    from marim_harness.config.external_cli import ActivityLedger

    attached: list = []
    ledger = ActivityLedger(attached.append)
    ledger.note_event(PartStartEvent(index=0, part=TextPart(content="")))
    # Parts alone never attach: a tool-free turn stays byte-identical.
    assert attached == [] and ledger.entries == [{"kind": "part", "index": 0}]
    ledger.note_activity([_call_event()])
    ledger.note_activity([_result_event(failed=True)])
    ledger.note_event(PartStartEvent(index=1, part=TextPart(content="")))
    assert len(attached) == 1 and attached[0] is ledger.entries  # the live list, not a copy
    assert ledger.entries == [
        {"kind": "part", "index": 0},
        {"kind": "call", "id": "t1", "name": "read_file", "args": {"path": "a"}},
        {"kind": "result", "id": "t1", "content": "ok", "outcome": "failed"},
        {"kind": "part", "index": 1},
    ]


def test_activity_ledger_caps_a_huge_result():
    from marim_harness.config.external_cli import MAX_RECORDED_RESULT_CHARS, ActivityLedger

    ledger = ActivityLedger(lambda entries: None)
    ledger.note_activity([_result_event(content="x" * (MAX_RECORDED_RESULT_CHARS + 500))])
    recorded = ledger.entries[0]["content"]
    assert recorded.startswith("x" * MAX_RECORDED_RESULT_CHARS)
    assert recorded.endswith("…[truncated 500 chars]")
    assert len(recorded) < MAX_RECORDED_RESULT_CHARS + 100


async def test_activity_ledger_recording_wraps_the_side_channel_and_keeps_none():
    from marim_harness.config.external_cli import ActivityLedger

    ledger = ActivityLedger(lambda entries: None)
    assert ledger.recording(None) is None  # fold mode: no side-channel, no ledger
    forwarded: list = []

    async def on_activity(events):
        forwarded.extend(events)

    wrapped = ledger.recording(on_activity)
    assert wrapped is not None
    await wrapped([_call_event("c")])
    assert [e.part.tool_call_id for e in forwarded] == ["c"]
    assert ledger.entries == [
        {"kind": "call", "id": "c", "name": "read_file", "args": {"path": "a"}}
    ]
