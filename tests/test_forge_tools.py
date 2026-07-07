from dataclasses import dataclass
from pathlib import Path

import pytest

from marim_harness.forge.models import CiRun, CiStatus, ForgeError, PullRequest
from marim_harness.tools import forge_tools as ft


@dataclass
class _WS:
    root: Path


@dataclass
class _Deps:
    workspace: _WS


class _Ctx:
    def __init__(self, root):
        self.deps = _Deps(_WS(root))


class StubBackend:
    """In-memory ForgeBackend — no CLI. Configured per test."""

    def __init__(self, prs=(), status=None, existing=None, created=None):
        self._prs = list(prs)
        self._status = status or CiStatus(overall="unknown")
        self._existing = existing
        self._created = created
        self.created_args = None

    async def list_prs(self, state, limit):
        return self._prs

    async def view_pr(self, number, branch):
        return self._existing

    async def ci_status(self, branch):
        return self._status

    async def create_pr(self, title, body, base, draft, head):
        self.created_args = (title, body, base, draft, head)
        return self._created

    async def checkout_pr(self, number, create_branch):
        return f"Checked out PR #{number}."


def _tool(ts, name):
    return ts.tools[name].function


def test_toolset_gating_flags():
    ts = ft.build_forge_toolset(StubBackend())
    assert ts.tools["create_pr"].requires_approval is True
    assert ts.tools["checkout_pr"].requires_approval is True
    for name in ("list_prs", "view_pr", "ci_status"):
        assert ts.tools[name].requires_approval is not True


@pytest.mark.anyio
async def test_list_prs_formats(monkeypatch, tmp_path):
    pr = PullRequest(number=51, title="T", state="open", head="f", base="master",
                     mergeable=True, url="u", ci="success")
    ts = ft.build_forge_toolset(StubBackend(prs=[pr]))
    out = await _tool(ts, "list_prs")(_Ctx(tmp_path), "open", 30)
    assert "#51" in out and "success" in out and "T" in out


@pytest.mark.anyio
async def test_ci_status_uses_current_branch(monkeypatch, tmp_path):
    monkeypatch.setattr(ft, "current_branch", _aret("feature/x"))
    st = CiStatus(overall="failure",
                  runs=(CiRun("build", "completed", "push", "feature/x", "t"),))
    ts = ft.build_forge_toolset(StubBackend(status=st))
    out = await _tool(ts, "ci_status")(_Ctx(tmp_path), None, None)
    assert "feature/x" in out and "failure" in out and "build" in out


@pytest.mark.anyio
async def test_create_pr_refuses_unpushed_branch(monkeypatch, tmp_path):
    monkeypatch.setattr(ft, "current_branch", _aret("feature/x"))
    monkeypatch.setattr(ft, "branch_pushed", _aret(False))
    ts = ft.build_forge_toolset(StubBackend())
    out = await _tool(ts, "create_pr")(_Ctx(tmp_path), "T", "B", None, False)
    assert "not pushed" in out and "git push" in out


@pytest.mark.anyio
async def test_create_pr_refuses_when_pr_exists(monkeypatch, tmp_path):
    monkeypatch.setattr(ft, "current_branch", _aret("feature/x"))
    monkeypatch.setattr(ft, "branch_pushed", _aret(True))
    existing = PullRequest(number=9, title="old", state="open", head="feature/x",
                           base="master", mergeable=True, url="u9", ci="pending")
    ts = ft.build_forge_toolset(StubBackend(existing=existing))
    out = await _tool(ts, "create_pr")(_Ctx(tmp_path), "T", "B", None, False)
    assert "already exists" in out and "#9" in out


@pytest.mark.anyio
async def test_create_pr_happy_path(monkeypatch, tmp_path):
    monkeypatch.setattr(ft, "current_branch", _aret("feature/x"))
    monkeypatch.setattr(ft, "branch_pushed", _aret(True))
    created = PullRequest(number=52, title="T", state="open", head="feature/x",
                          base="master", mergeable=True, url="u52", ci="pending")
    backend = StubBackend(existing=None, created=created)
    ts = ft.build_forge_toolset(backend)
    out = await _tool(ts, "create_pr")(_Ctx(tmp_path), "T", "B", None, False)
    assert "#52" in out and "u52" in out
    assert backend.created_args == ("T", "B", None, False, "feature/x")


@pytest.mark.anyio
async def test_tool_surfaces_forge_error(monkeypatch, tmp_path):
    class Boom(StubBackend):
        async def list_prs(self, state, limit):
            raise ForgeError("network down")
    ts = ft.build_forge_toolset(Boom())
    out = await _tool(ts, "list_prs")(_Ctx(tmp_path), "open", 30)
    assert "network down" in out


def test_forge_toolsets_gate(monkeypatch, tmp_path):
    monkeypatch.setattr(ft, "select_backend", lambda enabled, root: None)
    assert ft.forge_toolsets(False, tmp_path) == []
    monkeypatch.setattr(ft, "select_backend", lambda enabled, root: StubBackend())
    assert len(ft.forge_toolsets(True, tmp_path)) == 1


def _aret(value):
    async def _f(*args, **kwargs):
        return value
    return _f
