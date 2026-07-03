import asyncio
from pathlib import Path

import pytest

from tests.test_agent_sessions import _autoname_harness, _fake_titler


def test_clean_title_strips_noise():
    from marim_harness.compaction import clean_title

    assert clean_title('"Fix the bug"') == "Fix the bug"
    assert clean_title("Title: Add a feature") == "Add a feature"
    assert clean_title("Refactor parser\n\nignored") == "Refactor parser"
    assert clean_title("Do the thing.") == "Do the thing"
    assert clean_title("   ") == "Untitled session"


def test_clean_title_clamps_length():
    from marim_harness.compaction import clean_title

    out = clean_title("word " * 30)
    assert len(out) <= 51
    assert out.endswith("…")


def test_title_prompt_restates_task_in_message():
    # The titler must restate the task IN the user message (like the summarizer),
    # not rely on the system prompt alone — otherwise a model whose system prompt
    # is dominated by something else (e.g. claude -p, where our instruction is only
    # *appended* to Claude Code's own prompt) replies conversationally instead of
    # titling, and that reply becomes the session name.
    from marim_harness.compaction import _title_prompt

    out = _title_prompt("User: fix the parser off-by-one\nAssistant: done")
    assert "fix the parser off-by-one" in out
    assert "TRANSCRIPT" in out
    lowered = out.lower()
    assert "only the title" in lowered
    assert "do not reply conversationally" in lowered


@pytest.mark.anyio
async def test_make_titler_returns_clean_title():
    from pydantic_ai import Agent
    from pydantic_ai.models.test import TestModel

    from marim_harness.runtime.harness import make_titler

    run = await Agent(TestModel(), instructions="x").run("do a thing")
    history = run.all_messages()
    titler = make_titler(TestModel(custom_output_text='"Generated Title"'))
    assert await titler(history) == "Generated Title"


@pytest.mark.anyio
async def test_autoname_after_first_turn(tmp_path: Path):
    renames = []
    h = _autoname_harness(tmp_path, _fake_titler)
    h.session.on_rename = lambda old, new: renames.append((old, new))

    await h.run_turn("hello there")
    await h.session.wait_autoname()  # the rename runs in the background
    assert h.session.session_name == "Generated Title"
    assert h.session.store.auto_named is False
    assert renames and renames[-1][1] == "Generated Title"
    # persisted
    assert h.session.manager.store(h.session.store.session_id).name == "Generated Title"


@pytest.mark.anyio
async def test_explicitly_named_session_not_autorenamed(tmp_path: Path):
    h = _autoname_harness(tmp_path, _fake_titler, name="my project")
    await h.run_turn("hello")
    assert h.session.session_name == "my project"


@pytest.mark.anyio
async def test_autoname_happens_only_once(tmp_path: Path):
    calls = {"n": 0}

    async def counting_titler(messages):
        calls["n"] += 1
        return f"Title {calls['n']}"

    h = _autoname_harness(tmp_path, counting_titler)
    await h.run_turn("first")
    await h.session.wait_autoname()
    await h.run_turn("second")
    await h.session.wait_autoname()
    assert calls["n"] == 1
    assert h.session.session_name == "Title 1"


def _gated_titler(title: str):
    """A titler blocked on an event, so tests control exactly when the
    background autoname's LLM round-trip 'returns'."""
    gate = asyncio.Event()

    async def titler(messages) -> str:
        await gate.wait()
        return title

    return gate, titler


@pytest.mark.anyio
async def test_autoname_does_not_block_the_turn(tmp_path: Path):
    gate, titler = _gated_titler("Generated Title")
    h = _autoname_harness(tmp_path, titler)
    placeholder = h.session.session_name

    await h.run_turn("hello")
    # The turn returned while the titler is still blocked — naming is off the
    # turn's critical path.
    assert h.session.session_name == placeholder
    assert h.session.store.auto_named is True

    gate.set()
    await h.session.wait_autoname()
    assert h.session.session_name == "Generated Title"
    assert h.session.manager.store(h.session.store.session_id).name == "Generated Title"


@pytest.mark.anyio
async def test_new_session_cancels_inflight_autoname(tmp_path: Path):
    gate, titler = _gated_titler("Old Transcript Title")
    h = _autoname_harness(tmp_path, titler)
    old_store = h.session.store

    await h.run_turn("hello")
    h.session.new_session()  # switch away while the titler is still in flight
    gate.set()
    await h.session.wait_autoname()

    # The old transcript's title must not land on the new session; the old
    # session stays auto_named so a later resume retries.
    assert h.session.session_name != "Old Transcript Title"
    assert h.session.store.auto_named is True
    assert old_store.name != "Old Transcript Title"
    assert old_store.auto_named is True


@pytest.mark.anyio
async def test_autoname_apply_guard_on_store_rebind(tmp_path: Path):
    """The apply-side guard inside maybe_autoname itself: even without the
    cancel (e.g. a direct call), a store rebound during the titler await means
    the title belongs to another transcript and is dropped."""
    gate, titler = _gated_titler("Old Transcript Title")
    h = _autoname_harness(tmp_path, titler)
    old_store = h.session.store
    h.session.history = ["u1"]  # any non-empty history; the gated titler ignores it

    task = asyncio.ensure_future(h.session.maybe_autoname())
    await asyncio.sleep(0)  # let the worker capture the store and block on the gate
    h.session.store = h.session.manager.create()
    gate.set()
    await task

    assert old_store.name != "Old Transcript Title"
    assert h.session.store.name != "Old Transcript Title"


@pytest.mark.anyio
async def test_manual_rename_beats_inflight_autoname(tmp_path: Path):
    gate, titler = _gated_titler("Generated Title")
    h = _autoname_harness(tmp_path, titler)

    await h.run_turn("hello")
    await asyncio.sleep(0)  # let the background task start and block on the gate
    assert await h.rename_session("My Name") == "My Name"
    gate.set()
    await h.session.wait_autoname()

    # rename() flipped auto_named, so the generated title is dropped on apply.
    assert h.session.session_name == "My Name"


@pytest.mark.anyio
async def test_wait_and_cancel_autoname_are_noops_when_idle(tmp_path: Path):
    h = _autoname_harness(tmp_path, _fake_titler, name="named")
    await h.session.wait_autoname()
    h.session.cancel_autoname()


@pytest.mark.anyio
async def test_explicit_rename_never_persists_history(tmp_path: Path):
    """The TUI dispatches slash commands even mid-turn, so /name can run while
    the in-memory history holds a dirty approval-round state. The rename must
    patch only the name header on disk — the messages array stays untouched."""
    h = _autoname_harness(tmp_path, _fake_titler, name="start")
    await h.run_turn("do work")  # persists a clean history
    on_disk, _, _, _ = h.session.store.load()

    # Simulate the mid-approval state: in-memory history has grown past what
    # was persisted (and must not reach disk via the rename).
    h.session.history = list(h.session.history) + ["dangling tool call"]
    assert await h.rename_session("Mid-turn Name") == "Mid-turn Name"

    again = h.session.manager.store(h.session.store.session_id)
    assert again.name == "Mid-turn Name"
    messages, _, _, _ = again.load()
    assert len(messages) == len(on_disk)


@pytest.mark.anyio
async def test_rename_before_first_persist_lands_on_next_save(tmp_path: Path):
    """With no session file yet, rename's metadata patch is a no-op on disk;
    the in-memory name rides along with the next full persist."""
    h = _autoname_harness(tmp_path, _fake_titler, name="start")
    assert await h.rename_session("Early Name") == "Early Name"
    assert not h.session.store.path.exists()
    await h.run_turn("hello")  # the turn's persist carries the name
    assert h.session.manager.store(h.session.store.session_id).name == "Early Name"


@pytest.mark.anyio
async def test_rename_session_explicit_and_generated(tmp_path: Path):
    h = _autoname_harness(tmp_path, _fake_titler, name="start")
    # Explicit rename sets the name verbatim.
    assert await h.rename_session("Manual Name") == "Manual Name"
    assert h.session.session_name == "Manual Name"
    # Blank rename regenerates from the conversation via the titler.
    await h.run_turn("do work")
    assert await h.rename_session() == "Generated Title"
    assert h.session.session_name == "Generated Title"
