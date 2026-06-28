# tests/test_image_attachments.py
import pytest
from pydantic_ai.messages import BinaryContent, UserPromptPart

from marim_harness.runtime.harness import Harness
from marim_harness.tools.provider import BuiltinToolProvider
from tests.conftest import _make_deps


def _harness(tmp_path):
    from pydantic_ai.models.test import TestModel

    deps = _make_deps(tmp_path)
    return Harness(TestModel(call_tools=[]), BuiltinToolProvider(), deps,
                   instructions="test")


def _last_user_content(harness):
    for msg in reversed(harness.session.history):
        for part in getattr(msg, "parts", []):
            if isinstance(part, UserPromptPart):
                return part.content
    raise AssertionError("no user prompt recorded")


@pytest.mark.anyio
async def test_run_turn_attaches_binary_content(tmp_path):
    harness = _harness(tmp_path)
    await harness.run_turn("describe this", attachments=[(b"\x89PNGx", "image/png")])
    content = _last_user_content(harness)
    assert isinstance(content, list)
    assert any(isinstance(c, BinaryContent) and c.media_type == "image/png"
               for c in content)


@pytest.mark.anyio
async def test_run_turn_without_attachments_uses_plain_string(tmp_path):
    harness = _harness(tmp_path)
    await harness.run_turn("just text")
    content = _last_user_content(harness)
    assert isinstance(content, str)
