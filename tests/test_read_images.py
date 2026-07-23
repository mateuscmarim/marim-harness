"""Tool-layer tests for read_file image support: the vision gate and the
BinaryContent return (spec 2026-07-23-read-images-design)."""

from pathlib import Path
from types import SimpleNamespace

import pytest
from pydantic_ai.messages import BinaryContent

from marim_harness.runtime.deps import Deps, HarnessServices, UIHooks, WorkspaceConfig
from marim_harness.runtime.permissions import Mode
from marim_harness.tools import fs_tools

pytestmark = pytest.mark.anyio

PNG = b"\x89PNG\r\n\x1a\nfakepixels"


def _ctx(root: Path, *, gate=None, model_name="test-model"):
    deps = Deps(workspace=WorkspaceConfig(root=root, mode=Mode.auto), ui=UIHooks())
    deps.services = HarnessServices(supports_images=gate)
    return SimpleNamespace(deps=deps, model=SimpleNamespace(model_name=model_name))


async def test_image_returned_as_binary_content_without_gate(tmp_path):
    (tmp_path / "shot.png").write_bytes(PNG)
    out = await fs_tools.read_file(_ctx(tmp_path), "shot.png")
    assert isinstance(out, BinaryContent)
    assert out.data == PNG
    assert out.media_type == "image/png"


async def test_image_blocked_when_catalog_says_no_vision(tmp_path):
    async def gate(model_id):
        return False

    (tmp_path / "shot.png").write_bytes(PNG)
    out = await fs_tools.read_file(_ctx(tmp_path, gate=gate), "shot.png")
    assert isinstance(out, str)
    assert "does not accept image input" in out


async def test_image_sent_when_capability_unknown(tmp_path):
    async def gate(model_id):
        return None

    (tmp_path / "shot.png").write_bytes(PNG)
    out = await fs_tools.read_file(_ctx(tmp_path, gate=gate), "shot.png")
    assert isinstance(out, BinaryContent)


async def test_gate_receives_current_model_name(tmp_path):
    seen = []

    async def gate(model_id):
        seen.append(model_id)
        return True

    (tmp_path / "shot.png").write_bytes(PNG)
    await fs_tools.read_file(_ctx(tmp_path, gate=gate, model_name="acme/vlm-1"), "shot.png")
    assert seen == ["acme/vlm-1"]


async def test_text_read_still_returns_numbered_lines(tmp_path):
    (tmp_path / "a.txt").write_text("hello\n")
    out = await fs_tools.read_file(_ctx(tmp_path), "a.txt")
    assert isinstance(out, str)
    assert "1\thello" in out


async def test_ctx_without_model_attr_stays_optimistic(tmp_path):
    async def gate(model_id):  # pragma: no cover — must not be called
        raise AssertionError("gate must not be called without a model name")

    (tmp_path / "shot.png").write_bytes(PNG)
    ctx = SimpleNamespace(
        deps=_ctx(tmp_path).deps, model=SimpleNamespace(model_name=None)
    )
    ctx.deps.services = HarnessServices(supports_images=gate)
    out = await fs_tools.read_file(ctx, "shot.png")
    assert isinstance(out, BinaryContent)


def test_tool_result_text_renders_image_placeholder():
    from marim_harness.interfaces.tui.stream_render import tool_result_text

    img = BinaryContent(data=b"x" * 2048, media_type="image/png")
    assert tool_result_text(img) == "[image image/png, 2 KB]"
    assert tool_result_text([img, "note"]) == "[image image/png, 2 KB] note"
    assert tool_result_text("plain") == "plain"
    assert tool_result_text(None) == "None"
