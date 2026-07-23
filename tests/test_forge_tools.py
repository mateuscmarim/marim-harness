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


# Real Gitea clamps a page to ~api.MAX_RESPONSE_ITEMS (default 50) regardless of
# what --limit asks for; the stub models that so a `list_prs(limit>50)` scan can
# never see past the newest page (the bug the paged find in the backend fixes).
_SERVER_PAGE_CAP = 50


class StubBackend:
    """In-memory ForgeBackend — no CLI. Configured per test. ``list_prs`` models
    Gitea's server-side per-page clamp; ``find_open_pr_for_branch`` walks every
    open PR the way a real paged backend does."""

    def __init__(self, prs=(), status=None, existing=None, created=None):
        self._prs = list(prs)
        self._status = status or CiStatus(overall="unknown")
        self._existing = existing
        self._created = created
        self.created_args = None

    async def list_prs(self, state, limit):
        rows = self._prs if state == "all" else [p for p in self._prs if p.state == state]
        # Faithful clamp: at most _SERVER_PAGE_CAP rows come back however large
        # `limit` is — a caller that only has list_prs can't page past the newest
        # window, so a scan built on it misses an older PR.
        return rows[: min(limit, _SERVER_PAGE_CAP)]

    async def find_open_pr_for_branch(self, branch):
        # A real backend pages through *all* open PRs, so an old one is still
        # found; the stub scans its full list to model that.
        return next((p for p in self._prs if p.state == "open" and p.head == branch), None)

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
    out = await _tool(ts, "ci_status")(_Ctx(tmp_path), None)
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
    ts = ft.build_forge_toolset(StubBackend(prs=[existing]))
    out = await _tool(ts, "create_pr")(_Ctx(tmp_path), "T", "B", None, False)
    assert "already exists" in out and "#9" in out


@pytest.mark.anyio
async def test_create_pr_allows_when_only_closed_pr_exists(monkeypatch, tmp_path):
    monkeypatch.setattr(ft, "current_branch", _aret("feature/x"))
    monkeypatch.setattr(ft, "branch_pushed", _aret(True))
    old = PullRequest(number=9, title="old", state="merged", head="feature/x",
                      base="master", mergeable=True, url="u9", ci="success")
    created = PullRequest(number=52, title="T", state="open", head="feature/x",
                          base="master", mergeable=True, url="u52", ci="pending")
    backend = StubBackend(prs=[old], created=created)
    ts = ft.build_forge_toolset(backend)
    out = await _tool(ts, "create_pr")(_Ctx(tmp_path), "T", "B", None, False)
    assert "#52" in out and "u52" in out


@pytest.mark.anyio
async def test_create_pr_happy_path(monkeypatch, tmp_path):
    monkeypatch.setattr(ft, "current_branch", _aret("feature/x"))
    monkeypatch.setattr(ft, "branch_pushed", _aret(True))
    created = PullRequest(number=52, title="T", state="open", head="feature/x",
                          base="master", mergeable=True, url="u52", ci="pending")
    backend = StubBackend(created=created)
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


@pytest.mark.anyio
async def test_list_prs_tool_surfaces_malformed_tea_json(monkeypatch, tmp_path):
    # Finding 1 end-to-end: a real TeaBackend fed malformed tea JSON must return
    # a clean actionable string via the tool, never raise a bare exception.
    from marim_harness.forge import tea_backend as tb

    async def fake_run(args, cwd, timeout=20.0):
        return "null"  # would iterate None -> TypeError without the shape guard

    monkeypatch.setattr(tb, "_run_tea", fake_run)
    ts = ft.build_forge_toolset(tb.TeaBackend(tmp_path))
    out = await _tool(ts, "list_prs")(_Ctx(tmp_path), "open", 30)
    assert "Forge error" in out


@pytest.mark.anyio
async def test_create_pr_dup_check_pages_past_first_fifty(monkeypatch, tmp_path):
    # An existing open PR for the branch sits beyond the newest 50. The stub's
    # list_prs clamps at _SERVER_PAGE_CAP (real Gitea behaviour), so the old
    # grow-the-limit scan built on list_prs would miss #5 and open a duplicate —
    # this must FAIL against that. The backend's paged find_open_pr_for_branch
    # walks every open PR, so #5 is found and no duplicate is created.
    monkeypatch.setattr(ft, "current_branch", _aret("feature/old"))
    monkeypatch.setattr(ft, "branch_pushed", _aret(True))
    prs = [
        PullRequest(
            number=i, title=f"p{i}", state="open",
            head=("feature/old" if i == 5 else f"b{i}"), base="master",
            mergeable=True, url=f"u{i}", ci="success",
        )
        for i in range(60, 0, -1)  # newest-first; the branch's PR is old (#5)
    ]
    backend = StubBackend(prs=prs, created=None)
    ts = ft.build_forge_toolset(backend)
    out = await _tool(ts, "create_pr")(_Ctx(tmp_path), "T", "B", None, False)
    assert "already exists" in out and "#5" in out
    assert backend.created_args is None  # no duplicate was created


def test_forge_toolsets_gate(monkeypatch, tmp_path):
    monkeypatch.setattr(ft, "select_backend", lambda enabled, root: None)
    assert ft.forge_toolsets(False, tmp_path) == []
    monkeypatch.setattr(ft, "select_backend", lambda enabled, root: StubBackend())
    assert len(ft.forge_toolsets(True, tmp_path)) == 1


def _aret(value):
    async def _f(*args, **kwargs):
        return value
    return _f
