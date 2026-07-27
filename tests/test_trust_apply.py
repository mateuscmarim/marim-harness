"""The hot-apply seam: granting trust mid-session reloads the gated surface
without a rebuild."""

import json

import pytest

from marim_harness.mcp.manager import McpManager


@pytest.fixture(autouse=True)
def _isolated(monkeypatch, tmp_path):
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    monkeypatch.delenv("MARIM_TRUST_PROJECT_HOOKS", raising=False)


class _FakeServer:
    """Minimal MCPToolset stand-in: named, async-context-manageable."""

    def __init__(self, id):
        self.id = id
        self.entered = False

    async def __aenter__(self):
        self.entered = True
        return self

    async def __aexit__(self, *exc):
        return False


@pytest.mark.anyio
async def test_add_servers_connects_new_only():
    existing = _FakeServer("old")
    mgr = McpManager([existing], set())
    added = _FakeServer("new")
    dup = _FakeServer("old")
    await mgr.add_servers([dup, added])
    assert added.entered and not dup.entered
    assert "new" in mgr.configured_names()
    assert list(mgr.configured_names()).count("old") == 1
    # Status recording: mirror test_mcp_enable_status.py's assertion style
    # (mgr.mcp_status.connected / .failed), since add_servers reuses
    # enable_server's own bookkeeping after _connect_one.
    assert "new" in mgr.mcp_status.connected
    assert all(f[0] != "new" for f in mgr.mcp_status.failed)
    # The pre-existing "old" server was never touched by add_servers (it was
    # already configured), so it has no status entry from this call either.
    assert "old" not in mgr.mcp_status.connected
    assert all(f[0] != "old" for f in mgr.mcp_status.failed)


@pytest.mark.anyio
async def test_apply_project_trust_flips_state_and_reloads(tmp_path, monkeypatch):
    """Build a minimal harness against a workspace whose .marim ships a skill
    and a hooks.json; before apply nothing loads, after apply the TrustState
    is flipped, deps.hooks is a live HookRunner, and discovery sees the skill."""
    from marim_harness.runtime.bootstrap import build_harness
    from marim_harness.workspace import discover_skills

    marim = tmp_path / ".marim"
    (marim / "skills" / "deploy").mkdir(parents=True)
    (marim / "skills" / "deploy" / "SKILL.md").write_text(
        "---\nname: deploy\ndescription: d\n---\n")
    (marim / "hooks.json").write_text(json.dumps(
        {"hooks": {"SessionStart": [{"hooks": [{"type": "command", "command": "true"}]}]}}))
    monkeypatch.setenv("MARIM_PROVIDER", "local")
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "data"))  # isolate sessions
    harness = build_harness(tmp_path)
    assert harness.trust_prompt is not None
    assert harness.deps.trust.project is False
    assert harness.deps.hooks is None
    names = [s.name for s in discover_skills(
        tmp_path, trust_project=harness.deps.trust.project)]
    assert "deploy" not in names

    await harness.apply_project_trust()

    assert harness.deps.trust.project is True
    assert harness.trust_prompt is None
    assert harness.deps.hooks is not None
    names = [s.name for s in discover_skills(
        tmp_path, trust_project=harness.deps.trust.project)]
    assert "deploy" in names
    # Idempotent: a second call is a no-op, not an error.
    await harness.apply_project_trust()


@pytest.mark.anyio
async def test_revoke_flips_state_and_drops_project_hooks(tmp_path, monkeypatch):
    from marim_harness.runtime.bootstrap import build_harness

    marim = tmp_path / ".marim"
    marim.mkdir()
    (marim / "hooks.json").write_text(json.dumps(
        {"hooks": {"SessionStart": [{"hooks": [{"type": "command", "command": "true"}]}]}}))
    monkeypatch.setenv("MARIM_PROVIDER", "local")
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "data"))
    monkeypatch.setenv("MARIM_TRUST_PROJECT_HOOKS", "1")
    harness = build_harness(tmp_path)
    assert harness.deps.hooks is not None

    harness.revoke_project_trust()

    assert harness.deps.trust.project is False
    assert harness.deps.hooks is None  # only project hooks existed
