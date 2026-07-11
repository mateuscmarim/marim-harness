"""The summarizer/titler aux agents must never run on a *live* ``ClaudeCliModel``.

A claude-cli main model carries the live ``session_id``; an aux agent sharing it
would resume — and reply into — the user's real Claude session (dropping its own
instructions). bootstrap builds the aux agents on an ``ephemeral_clone`` for that
reason, but a runtime ``/model`` switch rebuilds them via
``SessionController.update_model``. Both paths route the model through the single
``aux_model_for`` helper so the clone can never be dropped on one path but kept on
the other.
"""

from marim_harness.config.claude_cli_model import ClaudeCliModel
from marim_harness.session import ctrl as ctrl_mod
from marim_harness.session.ctrl import SessionController, aux_model_for
from tests.conftest import _make_deps

# --- the shared helper -------------------------------------------------------


def test_aux_model_for_clones_a_claude_cli_model():
    raw = ClaudeCliModel("opus")
    raw.session_id = "LIVE-SESSION-123"  # the user's real Claude conversation
    aux = aux_model_for(raw, cwd="/ws")
    assert aux is not raw
    assert isinstance(aux, ClaudeCliModel)
    assert aux.ephemeral is True
    # The clone must NOT carry the live session id (that's the whole hazard).
    assert aux.session_id is None
    assert aux.cwd == "/ws"


def test_aux_model_for_passes_other_providers_through_unchanged():
    class _PlainModel:
        pass

    model = _PlainModel()
    assert aux_model_for(model, cwd="/ws") is model


# --- the runtime /model-switch path (update_model) ---------------------------


def _controller(tmp_path, **kw):
    deps = _make_deps(tmp_path)
    return SessionController(
        None, None, deps, 100_000, 20,
        summarizer=lambda h: "sum", titler=lambda h: "tit", **kw,
    )


def test_update_model_builds_aux_agents_on_a_clone_for_claude_cli(tmp_path, monkeypatch):
    """Switching the session model to a ClaudeCliModel mid-session must build the
    aux agents on a stateless CLONE, never the raw (session-carrying) instance."""
    ctrl = _controller(tmp_path)
    seen: dict = {}
    monkeypatch.setattr(
        ctrl_mod, "make_summarizer",
        lambda m: seen.__setitem__("summarizer", m) or "sum",
    )
    monkeypatch.setattr(
        ctrl_mod, "make_titler",
        lambda m: seen.__setitem__("titler", m) or "tit",
    )

    raw = ClaudeCliModel("opus")
    raw.session_id = "LIVE-SESSION-123"
    ctrl.update_model(raw)

    for role in ("summarizer", "titler"):
        aux = seen[role]
        assert aux is not raw, f"{role} built on the raw live model"
        assert isinstance(aux, ClaudeCliModel) and aux.ephemeral
        assert aux.session_id is None  # does not share the live session


def test_update_model_reuses_the_model_for_non_claude_cli(tmp_path, monkeypatch):
    """Every other provider reuses the one model object — no needless clone."""
    ctrl = _controller(tmp_path)
    seen: dict = {}
    monkeypatch.setattr(
        ctrl_mod, "make_summarizer",
        lambda m: seen.__setitem__("summarizer", m) or "sum",
    )
    monkeypatch.setattr(
        ctrl_mod, "make_titler",
        lambda m: seen.__setitem__("titler", m) or "tit",
    )

    class _PlainModel:
        pass

    model = _PlainModel()
    ctrl.update_model(model)
    assert seen["summarizer"] is model
    assert seen["titler"] is model


def test_update_model_leaves_a_none_aux_agent_none(tmp_path):
    """A None summarizer/titler stays None — update_model only rebuilds the
    aux agents that were originally configured."""
    deps = _make_deps(tmp_path)
    ctrl = SessionController(None, None, deps, 100_000, 20)  # no aux agents
    ctrl.update_model(ClaudeCliModel("opus"))
    assert ctrl.summarizer is None
    assert ctrl.titler is None
