import io
import json

import pytest

from marim_harness.interfaces.cli import models as models_cmd
from marim_harness.workspace import ModelEntry


class _FakeSource:
    def __init__(self, entries):
        self._entries = entries

    async def list_models(self):
        return self._entries


def _patch_source(monkeypatch, entries):
    monkeypatch.setattr(
        "marim_harness.interfaces.cli.models.MultiModelSource.from_env",
        classmethod(lambda cls: _FakeSource(entries)),
    )


def test_no_subcommand_returns_2():
    err = io.StringIO()
    assert models_cmd.main([], err=err) == 2
    assert err.getvalue().strip()


def test_list_text(monkeypatch):
    _patch_source(
        monkeypatch,
        [
            ModelEntry(id="openai/gpt-5.2", name="GPT-5.2", provider="openrouter"),
            ModelEntry(id="anthropic/claude-sonnet-4-6", name="Claude Sonnet 4.6",
                       provider="openrouter"),
        ],
    )
    out = io.StringIO()
    assert models_cmd.main(["list"], out=out) == 0
    text = out.getvalue()
    assert "openrouter:openai/gpt-5.2" in text
    assert "GPT-5.2" in text
    assert "openrouter:anthropic/claude-sonnet-4-6" in text
    assert "Claude Sonnet 4.6" in text


def test_list_json(monkeypatch):
    _patch_source(
        monkeypatch,
        [
            ModelEntry(id="openai/gpt-5.2", name="GPT-5.2", provider="openrouter"),
            ModelEntry(id="anthropic/claude-sonnet-4-6", name="Claude Sonnet 4.6",
                       provider="openrouter"),
        ],
    )
    out = io.StringIO()
    assert models_cmd.main(["list", "--json"], out=out) == 0
    arr = json.loads(out.getvalue())
    assert arr == [
        {"id": "openai/gpt-5.2", "name": "GPT-5.2", "provider": "openrouter"},
        {"id": "anthropic/claude-sonnet-4-6", "name": "Claude Sonnet 4.6",
         "provider": "openrouter"},
    ]


def test_list_empty_friendly_note(monkeypatch):
    """An empty result from all providers shows a provider-agnostic message."""
    _patch_source(monkeypatch, [])
    out = io.StringIO()
    assert models_cmd.main(["list"], out=out) == 0
    note = out.getvalue().strip().lower()
    assert note
    assert "configured providers" in note


def test_list_empty(monkeypatch):
    _patch_source(monkeypatch, [])
    out = io.StringIO()
    assert models_cmd.main(["list"], out=out) == 0
    assert out.getvalue().strip()


def test_list_empty_json(monkeypatch):
    _patch_source(monkeypatch, [])
    out = io.StringIO()
    assert models_cmd.main(["list", "--json"], out=out) == 0
    assert json.loads(out.getvalue()) == []


@pytest.fixture
def anyio_backend():
    return "asyncio"
