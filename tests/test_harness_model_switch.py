"""Harness lifecycle for external-CLI models: a model switch closes the
outgoing model, teardown closes the current one."""

from __future__ import annotations

import asyncio

import pytest
from pydantic_ai.models.function import FunctionModel

from marim_harness.config.external_cli import ExternalCliModel
from tests.conftest import _make_deps, _make_harness


class _Closable(ExternalCliModel):
    provider_id = "fake-cli"

    def __init__(self) -> None:
        super().__init__()
        self.closed = 0
        self.released = 0
        self.adopted: list[object] = []

    def adopt(self, previous) -> None:
        self.adopted.append(previous)

    def release_conversation(self) -> None:
        self.released += 1

    @property
    def model_name(self) -> str:
        return "fake"

    async def request(self, *a, **k):  # pragma: no cover - never driven here
        raise NotImplementedError

    async def aclose(self) -> None:
        self.closed += 1

    def ephemeral_clone(self, *, cwd: str) -> ExternalCliModel:
        # update_model() always routes the new model through aux_model_for()
        # (even when no summarizer/titler is configured, as here) — a plain
        # stub without this override would blow up on the base's
        # NotImplementedError before set_model ever reaches the close logic
        # under test.
        return self


class _Source:
    """A ModelSource stand-in: `build` hands out prebuilt models by id."""

    def __init__(self, models: dict) -> None:
        self._models = models

    def build(self, model_id: str):
        return self._models[model_id]

    def label(self, model_id: str) -> str:
        return model_id


def _dummy() -> FunctionModel:
    async def fn(messages, info):  # pragma: no cover
        raise NotImplementedError

    return FunctionModel(fn)


@pytest.mark.anyio
async def test_set_model_closes_the_outgoing_external_model(tmp_path):
    old = _Closable()
    new = _dummy()
    h = _make_harness(old, _make_deps(tmp_path))
    h.model_source = _Source({"old": old, "new": new})
    h.set_model("new", persist=False)
    await asyncio.sleep(0)  # the close is scheduled, not awaited inline
    assert old.closed == 1
    assert h.current_model is new


@pytest.mark.anyio
async def test_set_model_lets_the_new_model_adopt_the_old_before_closing_it(tmp_path):
    old, new = _Closable(), _Closable()
    h = _make_harness(old, _make_deps(tmp_path))
    h.model_source = _Source({"old": old, "new": new})
    h.set_model("new", persist=False)
    await asyncio.sleep(0)
    # adopt() runs synchronously inside set_model, before the scheduled
    # close, so claude-cli can move the live process across the switch and
    # leave the outgoing model nothing to close.
    assert new.adopted == [old] and old.closed == 1 and new.closed == 0


@pytest.mark.anyio
async def test_every_store_rebind_releases_the_cli_conversation(tmp_path):
    """A switch, /new and /clear each rebind the session store; the live
    provider-side conversation belongs to the session being left, so the
    model is told to let go before anything else (on a switch: before
    _apply_saved_model, so a same-provider model change has nothing stale to
    adopt)."""
    from marim_harness.session import SessionManager

    model = _Closable()
    manager = SessionManager(tmp_path / "ws", base_dir=tmp_path / "data")
    store_a = manager.create("A")
    h = _make_harness(model, _make_deps(tmp_path / "ws"), store=store_a, manager=manager)
    h.session.persist(force=True)
    store_b = manager.create("B")
    store_b.path.parent.mkdir(parents=True, exist_ok=True)
    store_b.path.write_text("{}")

    h.switch_session(store_b.session_id)
    assert model.released == 1
    h.new_session("C")
    assert model.released == 2
    h.reset()
    assert model.released == 3
    # Nothing here switched the model, so nothing closed it.
    assert model.closed == 0 and h.current_model is model


def test_base_release_conversation_and_adopt_keep_nothing():
    class _Plain(ExternalCliModel):
        @property
        def model_name(self) -> str:
            return "plain"

        async def request(self, *a, **k):  # pragma: no cover
            raise NotImplementedError

    assert _Plain().adopt(_Plain()) is None
    assert _Plain().release_conversation() is None


@pytest.mark.anyio
async def test_set_model_to_the_same_object_does_not_close_it(tmp_path):
    same = _Closable()
    h = _make_harness(same, _make_deps(tmp_path))
    h.model_source = _Source({"same": same})
    h.set_model("same", persist=False)
    await asyncio.sleep(0)
    assert same.closed == 0


@pytest.mark.anyio
async def test_harness_aclose_closes_any_external_model(tmp_path):
    model = _Closable()
    h = _make_harness(model, _make_deps(tmp_path))
    await h.aclose()
    assert model.closed == 1


def test_base_aclose_is_a_no_op():
    class _Plain(ExternalCliModel):
        provider_id = "plain"

        @property
        def model_name(self) -> str:
            return "p"

        async def request(self, *a, **k):  # pragma: no cover
            raise NotImplementedError

    asyncio.run(_Plain().aclose())
