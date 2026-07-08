from pathlib import Path

import pytest
from pydantic_ai.models.test import TestModel

from marim_harness.forge.models import CiStatus, PullRequest
from marim_harness.runtime.harness import Harness, HarnessConfig
from marim_harness.tools.provider import BuiltinToolProvider
from tests.conftest import _make_deps  # same helper test_provider uses


def _harness(tmp_path: Path, **cfg_kwargs) -> Harness:
    return Harness(
        TestModel(), BuiltinToolProvider(), _make_deps(tmp_path), "instructions",
        config=HarnessConfig(lsp_enabled=False, **cfg_kwargs),
    )


class _FakeForgeBackend:
    """Minimal ForgeBackend stub (see forge/backend.py) — only attachment is
    asserted here, so every method just raises."""

    async def list_prs(self, state: str, limit: int) -> list[PullRequest]:
        raise NotImplementedError

    async def view_pr(self, number: int | None, branch: str | None) -> PullRequest | None:
        raise NotImplementedError

    async def ci_status(self, branch: str) -> CiStatus:
        raise NotImplementedError

    async def create_pr(
        self, title: str, body: str, base: str | None, draft: bool, head: str
    ) -> PullRequest:
        raise NotImplementedError

    async def checkout_pr(self, number: int, create_branch: bool) -> str:
        raise NotImplementedError


@pytest.fixture
def fake_forge_backend():
    return _FakeForgeBackend()


def test_explicit_forge_backend_attaches_toolset(tmp_path, fake_forge_backend):
    """An explicit backend attaches forge tools even with no tea CLI configured."""
    h = _harness(tmp_path, forge_enabled=True, forge_backend=fake_forge_backend)
    toolset_tools = {n for ts in h.agent.toolsets for n in getattr(ts, "tools", {})}
    assert "list_prs" in toolset_tools  # any forge tool name proves attachment


def test_global_instructions_gate(tmp_path, monkeypatch):
    """global_instructions=False must not read the user-level instructions file."""
    import marim_harness.runtime.instructions as instr

    calls = []
    monkeypatch.setattr(instr, "load_global_instructions",
                        lambda: calls.append(1) or "")
    _harness(tmp_path, global_instructions=False)
    # Registration is closure-based; forcing instruction evaluation is not needed —
    # with the gate off the closure must not even be registered. Assert via the
    # agent's instruction-function count vs a gated-on harness. pydantic-ai keeps
    # registered @agent.instructions closures in the plain list Agent._instructions
    # (there is no public accessor); verified against the installed version with
    # `uv run python -c "from pydantic_ai import Agent; a=Agent('test'); print(a._instructions)"`.
    h_on = _harness(tmp_path, global_instructions=True)
    h_off = _harness(tmp_path, global_instructions=False)
    assert len(h_off.agent._instructions) == len(h_on.agent._instructions) - 1
