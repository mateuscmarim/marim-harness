"""The external-CLI thread ref (`cli_thread_id`) rides on the session header."""

from __future__ import annotations

from pathlib import Path

import pytest
from pydantic_ai.messages import ModelRequest, ModelResponse, TextPart, UserPromptPart
from pydantic_ai.usage import RunUsage

from marim_harness.config.external_cli import ExternalCliModel
from marim_harness.session.ctrl import SessionController
from marim_harness.session.store import SessionManager
from tests.conftest import _make_deps, _make_harness, _text_model


def _manager(tmp_path: Path) -> SessionManager:
    return SessionManager(tmp_path / "ws", base_dir=tmp_path / "data")


def _history() -> list:
    return [
        ModelRequest(parts=[UserPromptPart(content="hi")]),
        ModelResponse(parts=[TextPart(content="hello")]),
    ]


def test_cli_thread_id_roundtrips_through_save_and_save_meta(tmp_path: Path):
    mgr = _manager(tmp_path)
    store = mgr.create("Work")
    store.cli_thread_id = "codex-cli:thread-1"
    store.save(_history(), RunUsage(input_tokens=1, output_tokens=1))
    assert mgr.store(store.session_id).cli_thread_id == "codex-cli:thread-1"
    assert next(i for i in mgr.list() if i.id == store.session_id).cli_thread_id == (
        "codex-cli:thread-1"
    )

    store.cli_thread_id = "codex-cli:thread-2"
    store.save_meta()  # metadata-only patch, messages untouched
    again = mgr.store(store.session_id)
    assert again.cli_thread_id == "codex-cli:thread-2"
    messages, _, _, _, _ = again.load()
    assert len(messages) == 2


def test_new_session_does_not_inherit_thread_id(tmp_path: Path):
    mgr = _manager(tmp_path)
    first = mgr.create("A")
    first.cli_thread_id = "codex-cli:thread-1"
    first.save(_history(), RunUsage())
    second = mgr.create("B")  # inherits model/advisor/thinking — never the thread
    assert second.cli_thread_id is None


def test_controller_set_cli_thread_id_patches_meta(tmp_path: Path):
    mgr = _manager(tmp_path)
    store = mgr.create("Work")
    ctrl = SessionController(store, mgr, _make_deps(tmp_path), 100_000, 20)
    assert ctrl.saved_cli_thread_id is None
    ctrl.set_cli_thread_id("codex-cli:thread-1")  # no file yet -> forced clean persist
    assert store.path.exists()
    assert mgr.store(store.session_id).cli_thread_id == "codex-cli:thread-1"
    ctrl.set_cli_thread_id(None)
    assert mgr.store(store.session_id).cli_thread_id is None


def test_set_model_clears_thread_ref_when_provider_changes(tmp_path: Path):
    mgr = _manager(tmp_path)
    store = mgr.create("Work")
    ctrl = SessionController(store, mgr, _make_deps(tmp_path), 100_000, 20)
    ctrl.set_cli_thread_id("codex-cli:thread-1")
    ctrl.set_model("codex-cli:gpt-5.4-mini")  # same provider: the thread continues
    assert ctrl.saved_cli_thread_id == "codex-cli:thread-1"
    ctrl.set_model("openrouter:foo/bar")  # provider switch orphans the thread
    assert ctrl.saved_cli_thread_id is None
    assert mgr.store(store.session_id).cli_thread_id is None


class _Fake(ExternalCliModel):
    provider_id = "fake-cli"

    def __init__(self) -> None:
        super().__init__()
        self.steered: list[str] = []
        self.compacted = 0
        self.accept_steer = True

    @property
    def model_name(self) -> str:
        return "fake"

    def steer(self, text: str) -> bool:
        self.steered.append(text)
        return self.accept_steer

    async def compact_remote(self) -> None:
        self.compacted += 1

    async def request(self, messages, model_settings, model_request_parameters):  # pragma: no cover
        raise NotImplementedError


@pytest.mark.anyio
async def test_harness_wires_thread_ref_seams_to_the_session(tmp_path: Path):
    # A real store is required here: saved_cli_thread_id/set_cli_thread_id
    # follow the same store-is-the-source-of-truth convention as
    # saved_model_id/set_model (see test_saved_model_id_none_without_store) —
    # a bare _make_harness() with no store is a no-op by design, so this
    # round-trip needs an attached SessionStore, matching the pattern used
    # throughout the suite (e.g. tests/test_recovery.py, test_subagent_resume.py).
    from marim_harness.session import SessionStore

    store = SessionStore(
        path=tmp_path / "sessions" / "s.json",
        workspace_root=tmp_path,
        session_id="s",
        name="s",
    )
    harness = _make_harness(_text_model(), _make_deps(tmp_path), store=store)
    fake = _Fake()
    harness.wire_cli_model(fake)
    assert fake.session_ref_getter is not None and fake.on_session_ref is not None
    assert fake.session_ref_getter() is None
    fake.on_session_ref("codex-cli:thread-1")
    assert harness.session.saved_cli_thread_id == "codex-cli:thread-1"
    assert fake.session_ref_getter() == "codex-cli:thread-1"


@pytest.mark.anyio
async def test_harness_steer_short_circuits_to_external_model(tmp_path: Path, monkeypatch):
    harness = _make_harness(_text_model(), _make_deps(tmp_path))
    fake = _Fake()
    harness.wire_cli_model(fake)
    harness.current_model = fake
    buffered: list[str] = []
    monkeypatch.setattr(
        harness.turn_controller, "steer", lambda text, att=None: buffered.append(text)
    )
    harness.steer("go left")
    assert fake.steered == ["go left"] and buffered == []
    fake.accept_steer = False  # no live turn: the harness keeps its own buffering
    harness.steer("go right")
    assert buffered == ["go right"]


@pytest.mark.anyio
async def test_manual_compact_also_compacts_remote_thread(tmp_path: Path, monkeypatch):
    harness = _make_harness(_text_model(), _make_deps(tmp_path))
    fake = _Fake()
    harness.wire_cli_model(fake)
    harness.current_model = fake

    async def ok(*, instructions=None):
        return True

    monkeypatch.setattr(harness.turn_controller, "manual_compact", ok)
    assert await harness.manual_compact() is True
    assert fake.compacted == 1

    async def blocked(*, instructions=None):
        return False

    monkeypatch.setattr(harness.turn_controller, "manual_compact", blocked)
    assert await harness.manual_compact() is False
    assert fake.compacted == 1  # a blocked local compaction never touches the thread


@pytest.mark.anyio
async def test_aclose_closes_the_shared_codex_server(tmp_path: Path, monkeypatch):
    closed: list[bool] = []

    async def fake_close():
        closed.append(True)

    monkeypatch.setattr("marim_harness.codex.server.close_shared_server", fake_close)
    harness = _make_harness(_text_model(), _make_deps(tmp_path))
    await harness.aclose()
    assert closed == [True]
