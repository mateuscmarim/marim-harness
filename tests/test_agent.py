import asyncio
import functools
from pathlib import Path

import pytest
from pydantic_ai.messages import ModelResponse, TextPart
from pydantic_ai.models.function import FunctionModel

from marim_harness.lsp.manager import LspManager
from marim_harness.runtime.harness import Harness
from marim_harness.runtime.permissions import Mode
from marim_harness.tools.provider import BuiltinToolProvider
from tests.conftest import _edit_then_done_model, _make_deps, _make_harness


def _raising_model() -> FunctionModel:
    """A model that fails mid-turn (simulates an API outage, or — the reported
    case — a render error raised by the TUI's event_stream_handler)."""

    def fn(messages, info):
        raise RuntimeError("turn boom")

    return FunctionModel(fn)


def _fail_once_then_echo_model(exc: BaseException) -> FunctionModel:
    """Turn 1 raises ``exc``; every later turn echoes back the latest user
    prompt text it received, so a test can assert what was prepended."""
    state = {"n": 0}

    def fn(messages, info):
        state["n"] += 1
        if state["n"] == 1:
            raise exc
        latest = ""
        for m in messages:
            for p in getattr(m, "parts", []):
                if type(p).__name__ == "UserPromptPart":
                    latest = str(p.content)
        return ModelResponse(parts=[TextPart(content=latest)])

    return FunctionModel(fn)


def test_actionable_error_note_surfaces_only_model_fixable_failures():
    """Only failures the model itself can act on get a next-turn note. Harness
    or render bugs, cancellations, and transient infra (rate limits, 5xx) get
    None — re-prompting the model wouldn't help and would only add noise."""
    from pydantic_ai.exceptions import (
        ModelHTTPError,
        UnexpectedModelBehavior,
        UsageLimitExceeded,
    )
    from textual.markup import MarkupError

    from marim_harness.runtime.harness import _actionable_error_note

    # Not the model's to fix.
    assert _actionable_error_note(MarkupError("bad markup")) is None
    assert _actionable_error_note(RuntimeError("a render bug")) is None
    assert _actionable_error_note(asyncio.CancelledError()) is None
    assert (
        _actionable_error_note(ModelHTTPError(status_code=429, model_name="m")) is None
    )  # rate limit — transient
    assert (
        _actionable_error_note(ModelHTTPError(status_code=503, model_name="m")) is None
    )  # server error — transient

    # The model can adjust and continue from these.
    assert (
        _actionable_error_note(ModelHTTPError(status_code=400, model_name="m", body="too long"))
        is not None
    )
    assert _actionable_error_note(UnexpectedModelBehavior("Exceeded maximum retries")) is not None
    assert _actionable_error_note(UsageLimitExceeded("limit reached")) is not None


@pytest.mark.anyio
async def test_actionable_failure_is_surfaced_to_model_next_turn(tmp_path: Path):
    """After an actionable failure, the next turn's prompt carries a short note
    so the model knows the prior turn did not complete and can adjust."""
    from pydantic_ai.exceptions import UnexpectedModelBehavior

    deps = _make_deps(tmp_path)
    harness = _make_harness(
        _fail_once_then_echo_model(UnexpectedModelBehavior("Exceeded max retries")),
        deps,
    )
    with pytest.raises(UnexpectedModelBehavior):
        await harness.run_turn("first request")
    echoed = await harness.run_turn("second request")
    assert "did not complete" in echoed.result  # the note rode along
    assert "second request" in echoed.result  # ...prepended to the real prompt
    # And it is one-shot: a third, clean turn carries no stale note.
    again = await harness.run_turn("third request")
    assert "did not complete" not in again.result


@pytest.mark.anyio
async def test_non_actionable_failure_leaves_no_note(tmp_path: Path):
    """A plain harness/render failure must not pollute the next prompt — the
    model can't fix it, so surfacing it would only mislead."""
    deps = _make_deps(tmp_path)
    harness = _make_harness(_fail_once_then_echo_model(RuntimeError("render boom")), deps)
    with pytest.raises(RuntimeError):
        await harness.run_turn("first request")
    echoed = await harness.run_turn("second request")
    assert "did not complete" not in echoed.result
    # The date envelope wraps every turn now; the important thing is that no
    # error note from the failed first turn leaked into the second prompt.
    assert echoed.result.endswith("second request")


@pytest.mark.anyio
async def test_failed_turn_preserves_user_prompt_in_history(tmp_path: Path):
    """When a turn raises, the user's prompt must survive in history so the
    session can continue instead of forgetting the request entirely."""
    deps = _make_deps(tmp_path)
    harness = _make_harness(_raising_model(), deps)
    with pytest.raises(RuntimeError):
        await harness.run_turn("please remember this request")
    user_texts = [
        p.content
        for m in harness.session.history
        for p in getattr(m, "parts", [])
        if type(p).__name__ == "UserPromptPart"
    ]
    assert any("please remember this request" in str(t) for t in user_texts)


@pytest.mark.anyio
async def test_failed_turn_persists_so_a_new_harness_can_resume(tmp_path: Path):
    """A turn that fails must still be persisted to the store, so a resumed
    session sees the lost prompt rather than starting blank."""
    from marim_harness.session import SessionManager

    deps = _make_deps(tmp_path)
    store = SessionManager(tmp_path / "ws", base_dir=tmp_path / "data").create()
    harness = Harness(
        model=_raising_model(),
        provider=BuiltinToolProvider(),
        deps=deps,
        instructions="x",
        store=store,
    )
    with pytest.raises(RuntimeError):
        await harness.run_turn("a request that crashed the turn")

    resumed = Harness(
        model=_edit_then_done_model(),
        provider=BuiltinToolProvider(),
        deps=deps,
        instructions="x",
        store=store,
    )
    resumed.resume()
    user_texts = [
        p.content
        for m in resumed.session.history
        for p in getattr(m, "parts", [])
        if type(p).__name__ == "UserPromptPart"
    ]
    assert any("a request that crashed the turn" in str(t) for t in user_texts)


# ---------------------------------------------------------------------------
# LspManager lifecycle wiring
# ---------------------------------------------------------------------------


def _minimal_harness(tmp_path: Path):
    """Build a Harness with the simplest valid wiring for lifecycle tests."""
    from pydantic_ai.models.test import TestModel

    return Harness(
        TestModel(),
        BuiltinToolProvider(),
        _make_deps(tmp_path, mode=Mode.ask),
        instructions="test",
    )


def test_harness_wires_lsp_manager(tmp_path):
    h = _minimal_harness(tmp_path)
    assert isinstance(h.lsp, LspManager)
    assert h.deps.services.lsp is h.lsp


@pytest.mark.anyio
async def test_harness_aclose_shuts_down_lsp(tmp_path):
    h = _minimal_harness(tmp_path)
    closed = {"n": 0}

    async def fake_aclose():
        closed["n"] += 1

    h.lsp.aclose = fake_aclose  # type: ignore[method-assign]
    await h.aclose()
    assert closed["n"] == 1


# ---------------------------------------------------------------------------
# Model switching
# ---------------------------------------------------------------------------


def _named_model(model_id: str) -> FunctionModel:
    """A model whose every reply names the id it was built for, so a test can
    tell which model actually ran a turn."""

    def fn(messages, info):
        return ModelResponse(parts=[TextPart(content=f"from {model_id}")])

    return FunctionModel(fn)


def test_harness_rejects_config_mixed_with_legacy_kwargs(tmp_path: Path):
    """Passing both config= and legacy kwargs silently dropped the kwargs (the
    `config or HarnessConfig(**kwargs)` short-circuit). Reject it loudly instead
    of pretending to 'merge' them as the old docstring claimed."""
    from marim_harness.runtime.harness import HarnessConfig

    deps = _make_deps(tmp_path)
    with pytest.raises(TypeError):
        Harness(
            _named_model("m"),
            BuiltinToolProvider(),
            deps,
            "i",
            config=HarnessConfig(model_label="from-config"),
            model_label="from-kwargs",  # would be silently ignored before
        )


def test_harness_accepts_config_alone(tmp_path: Path):
    from marim_harness.runtime.harness import HarnessConfig

    deps = _make_deps(tmp_path)
    h = Harness(
        _named_model("m"),
        BuiltinToolProvider(),
        deps,
        "i",
        config=HarnessConfig(model_label="from-config"),
    )
    assert h.model_label == "from-config"


def test_harness_accepts_legacy_kwargs_alone(tmp_path: Path):
    deps = _make_deps(tmp_path)
    h = Harness(
        _named_model("m"),
        BuiltinToolProvider(),
        deps,
        "i",
        model_label="from-kwargs",
    )
    assert h.model_label == "from-kwargs"


class _FakeSource:
    """Stand-in for config.ModelSource: builds id-tagged models, no network."""

    def __init__(self) -> None:
        self.built: list[str] = []

    def build(self, model_id: str) -> FunctionModel:
        self.built.append(model_id)
        return _named_model(model_id)

    def label(self, model_id: str) -> str:
        return f"fake/{model_id}"

    @property
    def is_local(self) -> bool:
        return False

    async def list_models(self):
        return []


def _switch_harness(tmp_path, *, source=None, summarizer=None, titler=None):
    from marim_harness.runtime.harness import HarnessConfig
    from marim_harness.session import SessionManager

    deps = _make_deps(tmp_path)
    manager = SessionManager(tmp_path / "ws", base_dir=tmp_path / "data")
    return Harness(
        model=_named_model("startup"),
        provider=BuiltinToolProvider(),
        deps=deps,
        instructions="x",
        config=HarnessConfig(
            store=manager.create(),
            manager=manager,
            model_source=source,
            model_id="startup",
            summarizer=summarizer,
            titler=titler,
        ),
    )


async def _fake_titler(messages) -> str:
    return "Generated Title"


@pytest.mark.anyio
async def test_set_model_switches_model_and_label(tmp_path: Path):
    src = _FakeSource()
    h = _switch_harness(tmp_path, source=src)
    h.set_model("openai/gpt-5.2")
    assert h.model_id == "openai/gpt-5.2"
    assert h.model_label == "fake/openai/gpt-5.2"
    assert src.built == ["openai/gpt-5.2"]
    out = await h.run_turn("hello")
    assert out.result == "from openai/gpt-5.2"  # the new model actually ran the turn


@pytest.mark.anyio
async def test_set_model_rebuilds_configured_aux_agents(tmp_path: Path):
    async def summarizer(messages, instructions=None):
        return "s"

    h = _switch_harness(tmp_path, source=_FakeSource(), summarizer=summarizer, titler=_fake_titler)
    old_summarizer, old_titler = h.session.summarizer, h.session.titler
    h.set_model("openai/gpt-5.2")
    assert h.session.summarizer is not old_summarizer  # repointed at the new model
    assert h.session.titler is not old_titler


@pytest.mark.anyio
async def test_set_model_leaves_unconfigured_aux_alone(tmp_path: Path):
    h = _switch_harness(tmp_path, source=_FakeSource())  # no summarizer/titler
    h.set_model("openai/gpt-5.2")
    assert h.session.summarizer is None  # not fabricated
    assert h.session.titler is None


def test_set_model_without_source_is_noop(tmp_path: Path):
    deps = _make_deps(tmp_path)
    h = Harness(
        model=_named_model("startup"),
        provider=BuiltinToolProvider(),
        deps=deps,
        instructions="x",
        model_id="startup",
    )
    h.set_model("openai/gpt-5.2")  # no source -> nothing changes
    assert h.model_id == "startup"


def test_set_model_persists_to_session(tmp_path: Path):
    h = _switch_harness(tmp_path, source=_FakeSource())
    h.set_model("openai/gpt-5.2")
    assert h.session.store.model == "openai/gpt-5.2"
    assert h.session.manager.store(h.session.store.session_id).model == "openai/gpt-5.2"


def test_set_model_invalidates_discovered_context_windows(tmp_path: Path):
    """A /model switch must re-arm window discovery: on the local provider the
    new model JIT-loads, possibly at a different context size than anything
    probed before — stale windows would gate compaction/masking on the old
    model's limit. A spy pins the WIRING; invalidate() itself is unit-tested
    in test_context_limits.py."""
    h = _switch_harness(tmp_path, source=_FakeSource())
    calls = {"n": 0}

    class _SpyLimits:
        def invalidate(self):
            calls["n"] += 1

        def threshold(self, model_id):
            return 100_000

    h.session.limits = _SpyLimits()
    h.set_model("openai/gpt-5.2")
    assert calls["n"] == 1


@pytest.mark.anyio
async def test_switch_session_restores_its_model(tmp_path: Path):
    h = _switch_harness(tmp_path, source=_FakeSource())
    h.set_model("openai/gpt-5.2")
    alpha_id = h.session.store.session_id

    # A fresh session reverts to the startup model...
    h.new_session("beta")
    h.set_model("anthropic/claude-sonnet-4-6")
    assert h.model_id == "anthropic/claude-sonnet-4-6"

    # ...and switching back restores alpha's saved model.
    h.switch_session(alpha_id)
    assert h.model_id == "openai/gpt-5.2"
    assert h.model_label == "fake/openai/gpt-5.2"


# --- session-ownership claims following the active view (session/claim.py) ---
# A successful ``try_acquire(..., kind="probe")`` means the slot is FREE; None
# means someone still holds it. flock is per open-file-description, so a probe
# from THIS process is denied by the harness's own claim exactly as another
# process's would be — which is what makes these single-process assertions valid.


def test_switch_session_swaps_claims(tmp_path: Path):
    from marim_harness.session.claim import try_acquire

    h = _switch_harness(tmp_path)
    outgoing_path = h.session.store.path
    h.adopt_claim(try_acquire(outgoing_path, kind="tui"), kind="tui")

    beta = h.session.manager.create()  # a second session to switch to
    beta.path.parent.mkdir(parents=True, exist_ok=True)
    beta.path.write_text("{}")  # create() does not persist; the switch loads
    assert h.switch_session(beta.session_id) >= 0

    # The outgoing claim is released, the incoming one held.
    assert try_acquire(outgoing_path, kind="probe") is not None
    assert try_acquire(h.session.store.path, kind="probe") is None


def test_switch_session_refuses_claimed_target_without_touching_outgoing(tmp_path: Path):
    from marim_harness.session.claim import SessionClaimed, try_acquire

    h = _switch_harness(tmp_path)
    outgoing_id = h.session.store.session_id
    h.adopt_claim(try_acquire(h.session.store.path, kind="tui"), kind="tui")
    beta = h.session.manager.create()
    outsider = try_acquire(beta.path, kind="daemon", endpoint="http://127.0.0.1:8643")
    assert outsider is not None
    try:
        with pytest.raises(SessionClaimed) as excinfo:
            h.switch_session(beta.session_id)
        assert excinfo.value.holder is not None
        assert excinfo.value.holder.kind == "daemon"
        # Still on the outgoing session, its claim intact.
        assert h.session.store.session_id == outgoing_id
        assert try_acquire(h.session.store.path, kind="probe") is None
    finally:
        outsider.release()


def test_switch_session_failed_load_releases_the_tentative_claim(tmp_path: Path):
    from marim_harness.session.claim import try_acquire
    from marim_harness.session.store import SessionLoadError

    h = _switch_harness(tmp_path)
    h.adopt_claim(try_acquire(h.session.store.path, kind="tui"), kind="tui")
    beta = h.session.manager.create()
    beta.path.write_text("{corrupt")  # forces SessionLoadError on switch
    with pytest.raises(SessionLoadError):
        h.switch_session(beta.session_id)
    # The tentative claim on beta was released; outgoing still held.
    assert try_acquire(beta.path, kind="probe") is not None
    assert try_acquire(h.session.store.path, kind="probe") is None


def test_switch_session_failing_after_the_commit_keeps_the_target_claim(tmp_path: Path):
    """The body can raise AFTER the controller already moved onto the target
    (``_apply_saved_model`` builds a model that no longer constructs). The claim
    must follow where we actually landed: releasing the tentative claim here
    would leave the session we now drive unowned while we still held the one we
    left — two processes could then drive the target."""
    from marim_harness.session.claim import claim_path, try_acquire

    h = _switch_harness(tmp_path)
    outgoing_path = h.session.store.path
    h.adopt_claim(try_acquire(outgoing_path, kind="tui"), kind="tui")
    beta = h.session.manager.create()
    beta.path.parent.mkdir(parents=True, exist_ok=True)
    beta.path.write_text("{}")  # loads fine; the failure comes after the switch

    def boom() -> None:
        raise RuntimeError("saved model no longer builds")

    h._apply_saved_model = boom  # the first post-commit step in the body

    with pytest.raises(RuntimeError):
        h.switch_session(beta.session_id)

    # We are on beta, so beta is the session we own.
    assert h.session.store.session_id == beta.session_id
    assert h._claim is not None
    assert h._claim.path == claim_path(beta.path)
    assert try_acquire(beta.path, kind="probe") is None  # held by the harness
    assert try_acquire(outgoing_path, kind="probe") is not None  # the one we left, let go


def test_owns_session_reads_the_held_claim_not_the_current_store(tmp_path: Path):
    """The guard must verify the claim it actually holds. Inferred from
    ``store.session_id`` it answers about the wrong file the moment the two
    disagree — True for a session we never claimed (masking a real refusal) and
    False for the one we do hold."""
    from marim_harness.session.claim import try_acquire

    h = _switch_harness(tmp_path)
    ours = h.session.store
    h.adopt_claim(try_acquire(ours.path, kind="tui"), kind="tui")
    unclaimed = h.session.manager.create()
    h.session.store = unclaimed  # the store now points somewhere our claim doesn't

    assert h._owns_session(unclaimed.session_id) is False  # store says yes, the claim says no
    assert h._owns_session(ours.session_id) is True  # the session we truly hold


def test_switch_session_still_refuses_a_target_the_store_already_shows(tmp_path: Path):
    """The consequence of the guard above: with the store pointing at a session
    whose claim an outsider holds, the switch must still acquire — and be
    refused — instead of short-circuiting into the body and driving it
    alongside the holder."""
    from marim_harness.session.claim import SessionClaimed, try_acquire

    h = _switch_harness(tmp_path)
    h.adopt_claim(try_acquire(h.session.store.path, kind="tui"), kind="tui")
    theirs = h.session.manager.create()
    theirs.path.parent.mkdir(parents=True, exist_ok=True)
    theirs.path.write_text("{}")  # loadable, so only the claim can refuse the switch
    h.session.store = theirs
    outsider = try_acquire(theirs.path, kind="daemon")
    assert outsider is not None
    try:
        with pytest.raises(SessionClaimed):
            h.switch_session(theirs.session_id)
    finally:
        outsider.release()


def test_switch_session_to_the_session_we_already_own_is_not_a_self_refusal(tmp_path: Path):
    """The session picker pre-highlights the ACTIVE row, so re-selecting the
    current session is one keystroke away. Re-acquiring our own claim would be
    denied by our own lock, so the swap must be skipped rather than reporting
    ourselves as the holder."""
    from marim_harness.session.claim import try_acquire

    h = _switch_harness(tmp_path)
    current_id = h.session.store.session_id
    h.session.store.path.parent.mkdir(parents=True, exist_ok=True)
    h.session.store.path.write_text("{}")  # so the reload has something to load
    claim = try_acquire(h.session.store.path, kind="tui")
    h.adopt_claim(claim, kind="tui")

    assert h.switch_session(current_id) >= 0
    assert h._claim is claim  # same claim, never swapped out
    assert try_acquire(h.session.store.path, kind="probe") is None  # still held


def test_new_session_swaps_claims(tmp_path: Path):
    from marim_harness.session.claim import try_acquire

    h = _switch_harness(tmp_path)
    old_path = h.session.store.path
    h.adopt_claim(try_acquire(old_path, kind="tui"), kind="tui")
    h.new_session("fresh")
    assert try_acquire(old_path, kind="probe") is not None
    assert try_acquire(h.session.store.path, kind="probe") is None


def test_release_claim_is_idempotent(tmp_path: Path):
    from marim_harness.session.claim import try_acquire

    h = _switch_harness(tmp_path)
    h.adopt_claim(try_acquire(h.session.store.path, kind="tui"), kind="tui")
    h.release_claim()
    h.release_claim()  # second call is a no-op
    assert try_acquire(h.session.store.path, kind="probe") is not None


def test_adopt_claim_releases_the_previously_held_one(tmp_path: Path):
    """Ownership is one session at a time: adopting a second claim gives up the
    first, so a re-adoption can never strand the session it replaces."""
    from marim_harness.session.claim import try_acquire

    h = _switch_harness(tmp_path)
    first_path = h.session.store.path
    h.adopt_claim(try_acquire(first_path, kind="tui"), kind="tui")
    other = h.session.manager.create()
    h.adopt_claim(try_acquire(other.path, kind="headless"), kind="headless")
    assert h._claim_kind == "headless"
    assert try_acquire(first_path, kind="probe") is not None  # first one let go
    assert try_acquire(other.path, kind="probe") is None  # second one held


def test_build_collaborators_wires_full_graph(tmp_path):
    from pydantic_ai.models.function import FunctionModel

    from marim_harness.runtime.harness import Collaborators, HarnessConfig, build_collaborators
    from marim_harness.tools.provider import BuiltinToolProvider

    deps = _make_deps(tmp_path, mode=Mode.ask)
    provider = BuiltinToolProvider()
    model = FunctionModel(lambda messages, info: None)

    collab = build_collaborators(
        model,
        provider,
        deps,
        "instructions",
        HarnessConfig(lsp_enabled=True),
        get_model=lambda: model,
    )

    # Container is fully populated.
    assert isinstance(collab, Collaborators)
    assert collab.agent is not None
    assert collab.mcp is not None
    assert collab.lsp is not None  # lsp_enabled=True
    assert collab.session is not None
    assert collab.checkpoints is not None
    assert collab.hooks is not None
    assert collab.subagents is not None
    # The deps<->services late binding ran as part of wiring.
    assert deps.services.lsp is collab.lsp
    assert deps.services.turn_hooks is collab.hooks
    assert deps.services.run_subagent == collab.subagents.run
    assert deps.services.run_background_agent == collab.subagents.run_background


def test_build_collaborators_respects_lsp_disabled(tmp_path):
    from pydantic_ai.models.function import FunctionModel

    from marim_harness.runtime.harness import HarnessConfig, build_collaborators
    from marim_harness.tools.provider import BuiltinToolProvider

    deps = _make_deps(tmp_path, mode=Mode.ask)
    model = FunctionModel(lambda messages, info: None)
    collab = build_collaborators(
        model,
        BuiltinToolProvider(),
        deps,
        "i",
        HarnessConfig(lsp_enabled=False),
        get_model=lambda: model,
    )
    assert collab.lsp is None
    assert deps.services.lsp is None


async def _areturn(value, *args, **kwargs):
    """Async stub that ignores whatever it's called with and returns
    ``value``. Bound via ``functools.partial`` to build each of
    ``test_bind_ui_wires_all_callbacks``'s callback stubs without a fresh
    nested ``async def`` per callback — bind_ui only threads each one through
    to a specific attribute, so only identity (not behavior) is under test."""
    return value


def _snoop(*args, **kwargs) -> None:
    """Sync no-op stub for the two non-async bind_ui callbacks
    (on_tasks_changed / on_jobs_changed) — see ``_areturn``."""
    return None


def test_bind_ui_wires_all_callbacks(tmp_path):
    h = _minimal_harness(tmp_path)

    request_approval = functools.partial(_areturn, True)
    ask_user = functools.partial(_areturn, None)
    on_subagent_event = functools.partial(_areturn, None)
    on_subagent_model = functools.partial(_areturn, None)
    on_subagent_usage = functools.partial(_areturn, None)
    on_cli_activity = functools.partial(_areturn, None)
    on_tasks_changed = _snoop
    on_jobs_changed = _snoop
    on_compact = functools.partial(_areturn, None)
    on_compact_start = functools.partial(_areturn, None)
    on_rename = functools.partial(_areturn, "new")

    h.bind_ui(
        request_approval=request_approval,
        ask_user=ask_user,
        on_subagent_event=on_subagent_event,
        on_subagent_model=on_subagent_model,
        on_subagent_usage=on_subagent_usage,
        on_cli_activity=on_cli_activity,
        on_tasks_changed=on_tasks_changed,
        on_jobs_changed=on_jobs_changed,
        on_compact=on_compact,
        on_compact_start=on_compact_start,
        on_rename=on_rename,
    )

    assert h.deps.ui.request_approval is request_approval
    assert h.deps.ui.ask_user is ask_user
    assert h.deps.ui.on_subagent_event is on_subagent_event
    assert h.deps.ui.on_subagent_model is on_subagent_model
    assert h.deps.ui.on_subagent_usage is on_subagent_usage
    assert h.deps.ui.on_cli_activity is on_cli_activity
    assert h.deps.tasks.on_change is on_tasks_changed
    assert h.deps.jobs.on_change is on_jobs_changed
    assert h.session.on_compact is on_compact
    assert h.session.on_compact_start is on_compact_start
    assert h.session.on_rename is on_rename


def test_scratchpad_flag_gates_services_getter(tmp_path: Path, monkeypatch):
    """scratchpad_enabled=False must leave services.get_scratchpad None — the
    single point every consumer (prompt block, tool guard roots, approval
    bypass) degrades on. Enabled (the default) wires a live getter that yields
    the active session's dir."""
    from pydantic_ai.models.test import TestModel

    from marim_harness.runtime.harness import HarnessConfig
    from marim_harness.session import SessionManager

    monkeypatch.setattr(
        "marim_harness.workspace.scratchpad.scratchpad_base",
        lambda: tmp_path / "scratch-base",
    )
    store = SessionManager(tmp_path, base_dir=tmp_path / "sessions").create()

    off = Harness(
        TestModel(),
        BuiltinToolProvider(),
        _make_deps(tmp_path, mode=Mode.ask),
        "i",
        config=HarnessConfig(scratchpad_enabled=False, store=store),
    )
    assert off.deps.services.get_scratchpad is None

    on = Harness(
        TestModel(),
        BuiltinToolProvider(),
        _make_deps(tmp_path, mode=Mode.ask),
        "i",
        config=HarnessConfig(store=store),
    )
    getter = on.deps.services.get_scratchpad
    assert getter is not None
    path = getter()
    assert path is not None and path.name == "scratchpad"
    assert store.session_id == path.parent.name
