from pathlib import Path

import pytest

from marim_harness.forge import tea_backend as tb
from marim_harness.forge.models import ForgeError

PR_JSON = """[
  {"index": "51", "title": "refactor tools", "state": "merged", "author": "Mateus",
   "head": "refactor/tools", "base": "master", "mergeable": "false",
   "url": "https://git.marim.dev/x/pulls/51", "updated": "2026-07-05T09:21:25Z", "ci": "success"}
]"""

RUNS_JSON = """[
  {"id": "1093", "status": "completed", "workflow": "feat install", "branch": "master",
   "event": "push", "started": "2026-07-07T16:01:42Z", "duration": "8m"}
]"""


def test_list_prs_args_includes_fields_and_json():
    args = tb._list_prs_args("open", 30)
    assert args[:2] == ["pr", "list"]
    assert "--state" in args and "open" in args
    assert "--limit" in args and "30" in args
    assert "-o" in args and "json" in args
    assert "--fields" in args and tb.PR_FIELDS in args
    assert all(isinstance(a, str) for a in args)  # argv list, injection guard


def test_create_pr_args_optional_flags():
    base = tb._create_pr_args("T", "B", None, False, "feat")
    assert "--head" in base and "feat" in base
    assert "--title" in base and "T" in base
    assert "--description" in base and "B" in base
    assert "--base" not in base and "--draft" not in base
    full = tb._create_pr_args("T", "B", "master", True, "feat")
    assert "--base" in full and "master" in full
    assert "--draft" in full


def test_checkout_pr_args():
    assert tb._checkout_pr_args(7, True) == ["pr", "checkout", "7", "-b"]
    assert tb._checkout_pr_args(7, False) == ["pr", "checkout", "7"]


def test_map_pr_coerces_types_and_normalizes_ci():
    pr = tb._map_pr(tb._loads(PR_JSON)[0])
    assert pr.number == 51 and isinstance(pr.number, int)
    assert pr.mergeable is False
    assert pr.ci == "success"
    assert pr.head == "refactor/tools" and pr.base == "master"
    assert pr.url.endswith("/pulls/51")


def test_map_run_fields_and_none_conclusion():
    run = tb._map_run(tb._loads(RUNS_JSON)[0])
    assert run.workflow == "feat install"
    assert run.status == "completed"
    assert run.event == "push" and run.branch == "master"
    assert run.conclusion is None and run.url is None


def test_loads_raises_forgeerror_on_bad_json():
    with pytest.raises(ForgeError) as exc:
        tb._loads("not json{")
    assert "could not parse" in str(exc.value)


class _FakeProc:
    def __init__(self, out: bytes, err: bytes, code: int):
        self._out, self._err, self.returncode = out, err, code

    async def communicate(self):
        return self._out, self._err

    def kill(self):  # pragma: no cover - only hit on timeout path
        pass

    async def wait(self):  # pragma: no cover
        pass


def _patch_exec(monkeypatch, out=b"", err=b"", code=0):
    async def fake_exec(*args, **kwargs):
        return _FakeProc(out, err, code)
    monkeypatch.setattr(tb.asyncio, "create_subprocess_exec", fake_exec)


@pytest.mark.anyio
async def test_run_tea_returns_stdout_on_success(monkeypatch):
    _patch_exec(monkeypatch, out=b"[]")
    assert await tb._run_tea(["pr", "list"], Path(".")) == "[]"


@pytest.mark.anyio
async def test_run_tea_raises_with_stderr_on_nonzero(monkeypatch):
    _patch_exec(monkeypatch, err=b"boom: not a repo", code=1)
    with pytest.raises(ForgeError) as exc:
        await tb._run_tea(["pr", "list"], Path("."))
    assert "boom: not a repo" in str(exc.value)


@pytest.mark.anyio
async def test_run_tea_raises_when_tea_missing(monkeypatch):
    async def boom(*a, **k):
        raise FileNotFoundError("tea")
    monkeypatch.setattr(tb.asyncio, "create_subprocess_exec", boom)
    with pytest.raises(ForgeError):
        await tb._run_tea(["pr", "list"], Path("."))


def test_tea_available_false_when_not_on_path(monkeypatch):
    monkeypatch.setattr(tb.shutil, "which", lambda _: None)
    assert tb.tea_available() is False


def test_tea_available_true_when_path_and_config(monkeypatch, tmp_path):
    monkeypatch.setattr(tb.shutil, "which", lambda _: "/usr/bin/tea")
    cfg = tmp_path / "tea"
    cfg.mkdir()
    (cfg / "config.yml").write_text("logins: []\n")
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    assert tb.tea_available() is True


@pytest.mark.anyio
async def test_backend_list_prs_maps(monkeypatch):
    async def fake_run(args, cwd, timeout=20.0):
        return PR_JSON
    monkeypatch.setattr(tb, "_run_tea", fake_run)
    prs = await tb.TeaBackend(Path(".")).list_prs("all", 30)
    assert len(prs) == 1 and prs[0].number == 51


@pytest.mark.anyio
async def test_backend_view_pr_by_branch(monkeypatch):
    async def fake_run(args, cwd, timeout=20.0):
        return PR_JSON
    monkeypatch.setattr(tb, "_run_tea", fake_run)
    pr = await tb.TeaBackend(Path(".")).view_pr(None, "refactor/tools")
    assert pr is not None and pr.number == 51
    miss = await tb.TeaBackend(Path(".")).view_pr(None, "no-such")
    assert miss is None


@pytest.mark.anyio
async def test_backend_ci_status_overall_from_pr(monkeypatch):
    async def fake_run(args, cwd, timeout=20.0):
        return RUNS_JSON if args[0] == "actions" else PR_JSON
    monkeypatch.setattr(tb, "_run_tea", fake_run)
    st = await tb.TeaBackend(Path(".")).ci_status("refactor/tools")
    assert st.overall == "success"
    # runs are filtered by branch; master run excluded for this branch
    assert all(r.branch == "refactor/tools" for r in st.runs)
