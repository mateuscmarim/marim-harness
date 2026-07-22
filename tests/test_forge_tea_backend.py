import json
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


@pytest.mark.anyio
async def test_backend_create_pr_refetches_by_head(monkeypatch):
    calls = []

    async def fake_run(args, cwd, timeout=20.0):
        calls.append(args[:2])
        if args[:2] == ["pr", "create"]:
            return "created PR text output"  # tea prints text, not JSON; ignored
        return PR_JSON  # the re-fetch (list) call
    monkeypatch.setattr(tb, "_run_tea", fake_run)
    pr = await tb.TeaBackend(Path(".")).create_pr("T", "B", None, False, "refactor/tools")
    assert pr.number == 51
    assert ["pr", "create"] in calls


@pytest.mark.anyio
async def test_backend_create_pr_raises_when_not_refetchable(monkeypatch):
    async def fake_run(args, cwd, timeout=20.0):
        if args[:2] == ["pr", "create"]:
            return ""
        return PR_JSON  # only head 'refactor/tools' present; 'missing-branch' absent
    monkeypatch.setattr(tb, "_run_tea", fake_run)
    with pytest.raises(ForgeError):
        await tb.TeaBackend(Path(".")).create_pr("T", "B", None, False, "missing-branch")


@pytest.mark.anyio
async def test_backend_checkout_pr_returns_confirmation(monkeypatch):
    async def fake_run(args, cwd, timeout=20.0):
        return ""
    monkeypatch.setattr(tb, "_run_tea", fake_run)
    msg = await tb.TeaBackend(Path(".")).checkout_pr(7, True)
    assert "#7" in msg


# --- Finding 1: malformed/unexpected tea JSON becomes ForgeError, never a bare
# KeyError/ValueError/TypeError/AttributeError escaping past the tool handler.


def _patch_run(monkeypatch, raw, *, only_actions=None):
    async def fake_run(args, cwd, timeout=20.0):
        if only_actions is not None and args[0] == "actions":
            return only_actions
        return raw
    monkeypatch.setattr(tb, "_run_tea", fake_run)


@pytest.mark.anyio
async def test_backend_list_prs_null_payload_raises_forgeerror(monkeypatch):
    _patch_run(monkeypatch, "null")  # tea emits null on some versions / empty repo
    with pytest.raises(ForgeError):
        await tb.TeaBackend(Path(".")).list_prs("all", 30)


@pytest.mark.anyio
async def test_backend_list_prs_dict_payload_raises_forgeerror(monkeypatch):
    _patch_run(monkeypatch, '{"index": "1"}')  # object where a list is expected
    with pytest.raises(ForgeError):
        await tb.TeaBackend(Path(".")).list_prs("all", 30)


@pytest.mark.anyio
async def test_backend_list_prs_non_dict_element_raises_forgeerror(monkeypatch):
    _patch_run(monkeypatch, '["not-an-object"]')
    with pytest.raises(ForgeError):
        await tb.TeaBackend(Path(".")).list_prs("all", 30)


def test_map_pr_missing_index_raises_forgeerror():
    with pytest.raises(ForgeError) as exc:
        tb._map_pr({"title": "no index here"})
    assert "index" in str(exc.value)


def test_map_pr_non_numeric_index_raises_forgeerror():
    with pytest.raises(ForgeError) as exc:
        tb._map_pr({"index": "not-a-number"})
    assert "index" in str(exc.value)


@pytest.mark.anyio
async def test_backend_ci_status_null_runs_raises_forgeerror(monkeypatch):
    # PRs list is fine, but `actions runs` comes back null -> ForgeError, not
    # a bare TypeError from iterating None.
    _patch_run(monkeypatch, PR_JSON, only_actions="null")
    with pytest.raises(ForgeError):
        await tb.TeaBackend(Path(".")).ci_status("refactor/tools")


# --- Finding 2: paging past the newest 50 so an old PR stays findable.


def _pr_rows(indices):
    return [
        {"index": str(i), "title": f"pr{i}", "state": "open", "author": "a",
         "head": f"b{i}", "base": "master", "mergeable": "true",
         "url": f"u{i}", "updated": "t", "ci": "success"}
        for i in indices
    ]


def _paging_run(rows, *, cap=50):
    """Fake _run_tea modeling *real* Gitea paging: whatever ``--limit`` asks
    for is clamped server-side to ``cap`` (``api.MAX_RESPONSE_ITEMS``, default
    50 in real Gitea), and ``--page`` (1-indexed, default 1) selects a slice of
    that clamped page size — a growing ``--limit`` with no ``--page`` therefore
    keeps re-fetching the very same newest-``cap`` window. ``rows`` are
    newest-first."""
    async def fake_run(args, cwd, timeout=20.0):
        requested = int(args[args.index("--limit") + 1])
        page = int(args[args.index("--page") + 1]) if "--page" in args else 1
        size = min(requested, cap)
        start = (page - 1) * size
        return json.dumps(rows[start:start + size])
    return fake_run


@pytest.mark.anyio
async def test_view_pr_finds_old_pr_beyond_first_fifty(monkeypatch):
    # 60 PRs newest-first (index 60..1); target #5 is old, outside the newest 50.
    rows = _pr_rows(range(60, 0, -1))
    monkeypatch.setattr(tb, "_run_tea", _paging_run(rows))
    pr = await tb.TeaBackend(Path(".")).view_pr(5, None)
    assert pr is not None and pr.number == 5


@pytest.mark.anyio
async def test_view_pr_returns_none_when_absent_after_paging(monkeypatch):
    rows = _pr_rows(range(60, 0, -1))
    monkeypatch.setattr(tb, "_run_tea", _paging_run(rows))
    pr = await tb.TeaBackend(Path(".")).view_pr(999, None)
    assert pr is None  # walked every page to an empty one, so genuinely missing


@pytest.mark.anyio
async def test_view_pr_finds_pr_older_than_newest_50_on_page_capping_server(monkeypatch):
    # Regression for the real-Gitea clamp bug: the server always caps a page
    # at 50 regardless of --limit, so a naive "grow --limit and re-query"
    # strategy re-fetches the same newest-50 window forever and never sees
    # PR #1. With --page walking (the fix), #1 is on the second page (rows
    # 51-100) and must be found.
    rows = _pr_rows(range(100, 0, -1))  # PRs #100..#1, newest-first
    monkeypatch.setattr(tb, "_run_tea", _paging_run(rows, cap=50))
    pr = await tb.TeaBackend(Path(".")).view_pr(1, None)
    assert pr is not None and pr.number == 1


@pytest.mark.anyio
async def test_find_pr_pages_even_when_server_clamps_below_page_size(monkeypatch):
    # A server whose actual per-page cap (10) is *smaller* than what we ask
    # for (_PAGE_SIZE, 50): every page is "short" relative to our request, so
    # a short-page termination heuristic would stop after page 1 and miss an
    # older PR. Only empty-page termination is safe here.
    rows = _pr_rows(range(35, 0, -1))  # 35 PRs, so page 4 (rows 31-35) is short but non-empty
    monkeypatch.setattr(tb, "_run_tea", _paging_run(rows, cap=10))
    pr = await tb.TeaBackend(Path(".")).view_pr(1, None)
    assert pr is not None and pr.number == 1


@pytest.mark.anyio
async def test_find_pr_stops_at_max_pages_when_server_never_empties(monkeypatch):
    # A pathological server that always returns a full, non-empty page (e.g.
    # looping through the same items) gives the empty-page termination
    # nothing to key off of. The _MAX_PAGES safety net must still bound the
    # scan instead of paging forever: exactly _MAX_PAGES pages of
    # _PAGE_SIZE rows each (_MAX_PAGES * _PAGE_SIZE PRs scanned), then give up.
    calls: list[list[str]] = []

    async def fake_run(args, cwd, timeout=20.0):
        calls.append(args)
        limit = int(args[args.index("--limit") + 1])
        assert limit == tb._PAGE_SIZE
        return json.dumps(_pr_rows(range(1, limit + 1)))  # always a full page

    monkeypatch.setattr(tb, "_run_tea", fake_run)
    pr = await tb.TeaBackend(Path(".")).view_pr(999999, None)  # never present
    assert pr is None
    assert len(calls) == tb._MAX_PAGES
