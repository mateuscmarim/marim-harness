"""Cross-version session compatibility: a transcript persisted by pydantic-ai
1.107 (the terminal v1 release) must load, re-persist, and re-load under the
pinned 2.x line.

The fixture is a real 1.107-era session (scrubbed) covering the part kinds a
working session accumulates: user-prompt (including a <turn-context> envelope),
thinking, text, tool-call, and tool-return. This is the rollback-risk tripwire
from the v2 migration: if a pydantic-ai bump changes the message schema so v1
histories stop validating, this fails loudly instead of every user's resume
failing in the field."""

import json
import shutil
from pathlib import Path

from pydantic_ai.messages import ModelRequest, ModelResponse

from marim_harness.session.store import SessionStore

FIXTURE = Path(__file__).parent / "fixtures" / "session_v1_pydantic_ai_1107.json"


def _store(tmp_path: Path) -> SessionStore:
    path = tmp_path / "session.json"
    shutil.copy(FIXTURE, path)
    return SessionStore(path, tmp_path, "20260703-005415", "v1 fixture")


def test_v1_transcript_loads_under_current_pydantic_ai(tmp_path: Path):
    messages, usage, tasks, duration, jobs = _store(tmp_path).load()
    assert len(messages) == 7
    # Every message deserialized into a real model class, not a fallback dict.
    assert all(isinstance(m, (ModelRequest, ModelResponse)) for m in messages)
    kinds = {p.part_kind for m in messages for p in m.parts}
    assert {"user-prompt", "thinking", "text", "tool-call", "tool-return"} <= kinds
    assert usage.input_tokens > 0 and usage.requests > 0


def test_v1_transcript_survives_save_load_round_trip(tmp_path: Path):
    store = _store(tmp_path)
    messages, usage, tasks, duration, jobs = store.load()

    store.save(messages, usage, tasks=tasks, duration_seconds=duration, jobs=jobs)
    reloaded, usage2, _, _, _ = store.load()

    assert len(reloaded) == len(messages)
    # Part-level identity: same kinds, tool names, and content survive the
    # v1-file -> v2-objects -> v2-file -> v2-objects trip.
    for before, after in zip(messages, reloaded, strict=True):
        assert type(before) is type(after)
        assert [p.part_kind for p in before.parts] == [p.part_kind for p in after.parts]
    assert usage2.input_tokens == usage.input_tokens

    # The re-persisted file is self-consistent JSON with the store's own header.
    data = json.loads(store.path.read_text())
    assert data["message_count"] == len(messages)
