"""The advisor steering block is gated on the same seam as the tool itself."""

from types import SimpleNamespace

from marim_harness.runtime.deps import Deps, WorkspaceConfig
from marim_harness.runtime.instructions import _advisor_guidance


def _ctx(tmp_path):
    return SimpleNamespace(deps=Deps(workspace=WorkspaceConfig(root=tmp_path)))


def test_empty_when_no_advisor(tmp_path):
    assert _advisor_guidance(_ctx(tmp_path)) == ""


def test_guidance_when_advisor_configured(tmp_path):
    ctx = _ctx(tmp_path)

    async def advise(messages):
        return "x"

    ctx.deps.services.advise = advise
    text = _advisor_guidance(ctx)
    assert "advisor" in text
    assert "transcript" in text
