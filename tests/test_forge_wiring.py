from pydantic_ai import Agent

from marim_harness.config import load_config  # returns a ModelConfig
from marim_harness.tools.forge_tools import build_forge_toolset


class _StubBackend:
    async def list_prs(self, state, limit): return []
    async def view_pr(self, number, branch): return None
    async def ci_status(self, branch): ...
    async def create_pr(self, title, body, base, draft, head): ...
    async def checkout_pr(self, number, create_branch): return ""


def test_marim_forge_env_default_on(monkeypatch):
    monkeypatch.delenv("MARIM_FORGE", raising=False)
    assert load_config().forge_enabled is True


def test_marim_forge_env_off(monkeypatch):
    monkeypatch.setenv("MARIM_FORGE", "0")
    assert load_config().forge_enabled is False


def test_agent_carries_attached_forge_toolset():
    ts = build_forge_toolset(_StubBackend())
    agent = Agent("test", toolsets=[ts])
    assert ts in agent.toolsets
    assert "create_pr" in ts.tools and "list_prs" in ts.tools
