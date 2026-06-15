from pathlib import Path

from pydantic_ai import Agent
from pydantic_ai.models.test import TestModel
from pydantic_ai.usage import RunUsage

from marim_harness.session import SessionStore


def _history() -> list:
    return Agent(TestModel(), instructions="x").run_sync("hi").all_messages()


def test_save_and_load_roundtrip(tmp_path: Path):
    store = SessionStore(tmp_path / "ws", base_dir=tmp_path / "data")
    history = _history()
    usage = RunUsage(input_tokens=12, output_tokens=8)
    store.save(history, usage)

    messages, restored = store.load()
    assert len(messages) == len(history)
    assert type(messages[0]).__name__ == type(history[0]).__name__
    assert restored.input_tokens == 12
    assert restored.output_tokens == 8
    assert restored.total_tokens == 20


def test_load_missing_returns_empty(tmp_path: Path):
    store = SessionStore(tmp_path / "ws", base_dir=tmp_path / "data")
    messages, usage = store.load()
    assert messages == []
    assert usage.total_tokens == 0


def test_path_is_workspace_specific(tmp_path: Path):
    base = tmp_path / "data"
    a = SessionStore(tmp_path / "ws-a", base_dir=base)
    b = SessionStore(tmp_path / "ws-b", base_dir=base)
    a_again = SessionStore(tmp_path / "ws-a", base_dir=base)
    assert a.path != b.path
    assert a.path == a_again.path  # stable per workspace


def test_clear_removes_file(tmp_path: Path):
    store = SessionStore(tmp_path / "ws", base_dir=tmp_path / "data")
    store.save(_history(), RunUsage())
    assert store.path.exists()
    store.clear()
    assert not store.path.exists()
    store.clear()  # idempotent
