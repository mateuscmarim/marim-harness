"""The ExternalCliModel base: one seam set shared by claude-cli and codex-cli."""

from __future__ import annotations

from marim_harness.config import claude_cli_model as ccm
from marim_harness.config.claude_cli_model import ClaudeCliModel
from marim_harness.config.external_cli import CliModelError, ExternalCliModel, TextFolder
from marim_harness.session.ctrl import aux_model_for
from tests.conftest import _make_deps, _make_harness, _text_model


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


def test_claude_cli_model_is_an_external_cli_model():
    assert issubclass(ClaudeCliModel, ExternalCliModel)
    assert ClaudeCliModel.provider_id == "claude-cli"
    assert ClaudeCliModel("x").system == "claude-cli"
    # Backwards-compatible names still resolve from the old module.
    assert ccm.CliModelError is CliModelError
    assert ccm._TextFolder is TextFolder


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
    assert m.on_subagent is harness.deps.ui.on_subagent_event
    assert m.on_subagent_model is harness.deps.ui.on_subagent_model
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
