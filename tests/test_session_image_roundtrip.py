"""Integration tests: a binary tool return survives a REAL SessionStore
save/load round trip.

The unit tests for externalize/rehydrate hand-build serialized dicts; these
pin the full pipeline instead — dump_python → externalize → json → rehydrate →
``ModelMessagesTypeAdapter.validate_python`` — so a pydantic-ai upgrade that
changes the multimodal discriminator (how a ``{"kind": "binary", ...}`` dict
revalidates into ``BinaryContent``) breaks loudly here instead of corrupting
resumed sessions."""

import shutil
from pathlib import Path

import pytest
from pydantic_ai.messages import (
    BinaryContent,
    ModelRequest,
    ModelResponse,
    TextPart,
    ToolCallPart,
    ToolReturnPart,
    UserPromptPart,
)
from pydantic_ai.usage import RunUsage

from marim_harness.session import SessionStore

PNG = b"\x89PNG\r\n\x1a\n" + b"p" * 4096


@pytest.fixture(autouse=True)
def _isolated_cache(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("MARIM_IMAGE_CACHE_DIR", str(tmp_path / "cache"))


def _store(tmp_path: Path) -> SessionStore:
    return SessionStore(path=tmp_path / "s.json", workspace_root=tmp_path,
                        session_id="sid", name="s")


def _history(content):
    return [
        ModelRequest(parts=[UserPromptPart(content="look at the image")]),
        ModelResponse(parts=[
            ToolCallPart(tool_name="read_file", args={"path": "shot.png"},
                         tool_call_id="c1"),
        ]),
        ModelRequest(parts=[
            ToolReturnPart(tool_name="read_file", content=content,
                           tool_call_id="c1"),
        ]),
        ModelResponse(parts=[TextPart(content="a red square")]),
    ]


def test_binary_tool_return_round_trips_through_save_load(tmp_path: Path):
    img = BinaryContent(data=PNG, media_type="image/png")
    store = _store(tmp_path)
    store.save(_history(img), RunUsage())
    # The on-disk file must hold a cache ref, never the base64 body.
    raw = store.path.read_text()
    assert "marim-image-cache://" in raw
    assert "p" * 100 not in raw

    messages, *_ = _store(tmp_path).load()
    part = messages[2].parts[0]
    assert isinstance(part, ToolReturnPart)
    assert isinstance(part.content, BinaryContent)
    assert part.content.data == PNG
    assert part.content.media_type == "image/png"


def test_list_content_with_binary_round_trips(tmp_path: Path):
    img = BinaryContent(data=PNG, media_type="image/png")
    store = _store(tmp_path)
    store.save(_history([img, "and a caption"]), RunUsage())

    messages, *_ = _store(tmp_path).load()
    content = messages[2].parts[0].content
    assert isinstance(content, list)
    assert isinstance(content[0], BinaryContent)
    assert content[0].data == PNG
    assert content[1] == "and a caption"


def test_list_content_cache_miss_degrades_to_placeholder(tmp_path: Path):
    """A vanished cache file must degrade that ONE item to the placeholder
    string and leave its list siblings intact — the session still loads."""
    img = BinaryContent(data=PNG, media_type="image/png")
    store = _store(tmp_path)
    store.save(_history([img, "and a caption"]), RunUsage())
    shutil.rmtree(tmp_path / "cache")

    messages, *_ = _store(tmp_path).load()
    content = messages[2].parts[0].content
    assert content[0] == "[image unavailable]"
    assert content[1] == "and a caption"
