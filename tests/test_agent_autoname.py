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
    await h.run_turn("second")
    assert calls["n"] == 1
    assert h.session.session_name == "Title 1"


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
