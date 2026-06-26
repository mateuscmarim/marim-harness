import io
import json

import pytest

from marim_harness.interfaces.cli import models as models_cmd
from marim_harness.workspace import ModelEntry


class _FakeSource:
    def __init__(self, entries, is_local=False):
        self._entries = entries
        self.is_local = is_local

    async def list_models(self):
        return self._entries


def _patch_source(monkeypatch, entries, is_local=False):
    monkeypatch.setattr(
        models_cmd,
        "ModelSource",
        lambda cfg: _FakeSource(entries, is_local=is_local),
    )


def test_no_subcommand_returns_2():
    err = io.StringIO()
    assert models_cmd.main([], err=err) == 2
    assert err.getvalue().strip()


def test_list_text(monkeypatch):
    _patch_source(
        monkeypatch,
        [
            ModelEntry(id="openai/gpt-5.2", name="GPT-5.2"),
            ModelEntry(id="anthropic/claude-sonnet-4-6", name="Claude Sonnet 4.6"),
        ],
    )
    out = io.StringIO()
    assert models_cmd.main(["list"], out=out) == 0
    text = out.getvalue()
    assert "openai/gpt-5.2" in text
    assert "GPT-5.2" in text
    assert "anthropic/claude-sonnet-4-6" in text
    assert "Claude Sonnet 4.6" in text


def test_list_json(monkeypatch):
    _patch_source(
        monkeypatch,
        [
            ModelEntry(id="openai/gpt-5.2", name="GPT-5.2"),
            ModelEntry(id="anthropic/claude-sonnet-4-6", name="Claude Sonnet 4.6"),
        ],
    )
    out = io.StringIO()
    assert models_cmd.main(["list", "--json"], out=out) == 0
    arr = json.loads(out.getvalue())
    assert arr == [
        {"id": "openai/gpt-5.2", "name": "GPT-5.2"},
        {"id": "anthropic/claude-sonnet-4-6", "name": "Claude Sonnet 4.6"},
    ]


def test_list_local_friendly_note(monkeypatch):
    """An empty local result now means the server was unreachable (local DOES
    have a catalog when up), so the note should point at the server, not claim
    local has no catalog."""
    _patch_source(monkeypatch, [], is_local=True)
    out = io.StringIO()
    assert models_cmd.main(["list"], out=out) == 0
    note = out.getvalue().strip().lower()
    assert note
    assert "no catalog for local" not in note
    assert "server" in note


def test_list_empty(monkeypatch):
    _patch_source(monkeypatch, [], is_local=False)
    out = io.StringIO()
    assert models_cmd.main(["list"], out=out) == 0
    assert out.getvalue().strip()


def test_list_empty_json(monkeypatch):
    _patch_source(monkeypatch, [], is_local=False)
    out = io.StringIO()
    assert models_cmd.main(["list", "--json"], out=out) == 0
    assert json.loads(out.getvalue()) == []


@pytest.fixture
def anyio_backend():
    return "asyncio"
