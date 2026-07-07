# Forge (tea) Toolset Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Give marim an agent-facing capability to work with the project's forge (Gitea via `tea`) — list/view PRs, check CI, open and check out PRs — as a Pydantic AI `FunctionToolset` behind a pluggable `ForgeBackend` seam.

**Architecture:** A `forge/` subsystem holds forge-neutral models, a `ForgeBackend` protocol, and one `TeaBackend` implementation that shells out to `tea … --output json` and maps its output into the neutral models. Five forge-agnostic tools (`tools/forge_tools.py`) close over the selected backend; read tools are ungated, `create_pr`/`checkout_pr` gate for approval. A `select_backend` decision (config flag + tea availability) attaches the toolset at build time. A future `gh` backend is one new file plus one `select_backend` branch.

**Tech Stack:** Python ≥3.10, Pydantic AI 1.107.0 (`FunctionToolset`, `RunContext`), `asyncio.create_subprocess_exec`, `tea` v0.14.2 CLI, pytest.

## Global Constraints

- Python `>=3.10`; no 3.11+-only syntax (`X | Y` unions are fine via `from __future__ import annotations`).
- Ruff line length 100; lint set `E,F,I,UP,B,SIM` (import sorting enforced). Run `uv run ruff check src tests`.
- Type-check clean under `uv run pyright` (standard mode, src only).
- Use `uv` for everything (`uv run pytest`, never bare `python`/`pytest`/`pip`).
- Tests never hit the network or invoke real `tea`/`git` — subprocess layers are always monkeypatched or stubbed.
- Match the CI order locally before claiming done: `uv run ruff check src tests` → `uv run pyright` → `uv run pytest`.
- Tool docstrings are model-facing product copy — write them as concise tool descriptions.
- Commit footer on every commit:
  ```
  Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>
  Claude-Session: https://claude.ai/code/session_017n1ei7XPReCcBAMwHJQHin
  ```

## File Structure

- Create `src/marim_harness/forge/__init__.py` — package marker (empty).
- Create `src/marim_harness/forge/models.py` — neutral `PullRequest`, `CiRun`, `CiStatus`, `ForgeError`, `normalize_ci`.
- Create `src/marim_harness/forge/backend.py` — `ForgeBackend` Protocol (the seam).
- Create `src/marim_harness/forge/tea_backend.py` — pure argv builders + JSON mappers, `_run_tea`/`_loads`, `tea_available`, `TeaBackend`.
- Create `src/marim_harness/forge/gitref.py` — `current_branch`, `branch_pushed` (git helpers).
- Create `src/marim_harness/forge/select.py` — `select_backend`.
- Create `src/marim_harness/tools/forge_tools.py` — the 5 tools, `build_forge_toolset`, `forge_toolsets`.
- Modify `src/marim_harness/config/model.py` — add `forge_enabled` field + `_bool_env("MARIM_FORGE", True)`.
- Modify `src/marim_harness/runtime/harness.py` — `HarnessConfig.forge_enabled`; attach `toolsets=` in `build_collaborators`.
- Modify `src/marim_harness/runtime/bootstrap.py` — thread `forge_enabled` into `HarnessConfig`.
- Modify `CLAUDE.md`, `.env.example` — document the forge toolset + `MARIM_FORGE`.
- Tests: `tests/test_forge_models.py`, `tests/test_forge_tea_backend.py`, `tests/test_forge_gitref.py`, `tests/test_forge_select.py`, `tests/test_forge_tools.py`, `tests/test_forge_wiring.py`.

---

### Task 1: Neutral models + ForgeBackend protocol

**Files:**
- Create: `src/marim_harness/forge/__init__.py`
- Create: `src/marim_harness/forge/models.py`
- Create: `src/marim_harness/forge/backend.py`
- Test: `tests/test_forge_models.py`

**Interfaces:**
- Produces:
  - `ForgeError(Exception)`
  - `normalize_ci(raw: str | None) -> str` → one of `"success" | "failure" | "pending" | "unknown"`
  - `@dataclass(frozen=True) PullRequest(number: int, title: str, state: str, head: str, base: str, mergeable: bool, url: str, ci: str, author: str = "", updated: str = "")`
  - `@dataclass(frozen=True) CiRun(workflow: str, status: str, event: str, branch: str, started: str, conclusion: str | None = None, url: str | None = None)`
  - `@dataclass(frozen=True) CiStatus(overall: str, runs: tuple[CiRun, ...] = ())`
  - `class ForgeBackend(Protocol)` with async methods: `list_prs(state, limit) -> list[PullRequest]`, `view_pr(number, branch) -> PullRequest | None`, `ci_status(branch) -> CiStatus`, `create_pr(title, body, base, draft, head) -> PullRequest`, `checkout_pr(number, create_branch) -> str`.

- [ ] **Step 1: Write the failing test**

`tests/test_forge_models.py`:
```python
from marim_harness.forge.models import CiStatus, ForgeError, PullRequest, normalize_ci


def test_normalize_ci_maps_known_and_unknown():
    assert normalize_ci("success") == "success"
    assert normalize_ci("SUCCESS") == "success"
    assert normalize_ci("failure") == "failure"
    assert normalize_ci("error") == "failure"
    assert normalize_ci("pending") == "pending"
    assert normalize_ci("") == "unknown"
    assert normalize_ci(None) == "unknown"
    assert normalize_ci("weird") == "unknown"


def test_pullrequest_is_frozen_with_defaults():
    pr = PullRequest(number=51, title="t", state="open", head="feat",
                     base="master", mergeable=True, url="http://x", ci="success")
    assert pr.author == "" and pr.updated == ""
    assert pr.number == 51 and pr.mergeable is True


def test_forge_error_is_exception():
    assert issubclass(ForgeError, Exception)
    assert str(ForgeError("boom")) == "boom"


def test_cistatus_defaults_empty_runs():
    assert CiStatus(overall="unknown").runs == ()
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest --no-cov tests/test_forge_models.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'marim_harness.forge'`.

- [ ] **Step 3: Write minimal implementation**

`src/marim_harness/forge/__init__.py`: empty file.

`src/marim_harness/forge/models.py`:
```python
"""Forge-neutral value types shared by every ForgeBackend implementation.

Nothing tea- or gh-specific lives here: a backend maps its CLI's output into
these types, and the tool layer consumes only these. That invariance is what
lets a future gh backend drop in without touching the tools or their tests.
"""

from __future__ import annotations

from dataclasses import dataclass


class ForgeError(Exception):
    """A forge CLI call failed, timed out, or returned unparseable output.

    Carries a model-actionable message (typically the CLI's stderr) so the tool
    layer can surface it verbatim rather than a traceback."""


# tea's PR `ci` field / a commit-status state -> our normalized vocabulary.
_CI_MAP = {
    "success": "success",
    "failure": "failure",
    "error": "failure",
    "pending": "pending",
    "": "unknown",
}


def normalize_ci(raw: str | None) -> str:
    """Normalize a backend CI/commit-status string to
    ``success|failure|pending|unknown``. Unrecognized/empty/None -> ``unknown``."""
    return _CI_MAP.get((raw or "").strip().lower(), "unknown")


@dataclass(frozen=True)
class CiRun:
    """One CI/workflow run. ``conclusion`` and ``url`` are optional because the
    tea backend cannot expose per-run pass/fail (tea's ``actions runs`` reports
    only a run ``status`` like ``completed``); a future gh backend fills them."""

    workflow: str
    status: str
    event: str
    branch: str
    started: str
    conclusion: str | None = None
    url: str | None = None


@dataclass(frozen=True)
class CiStatus:
    """Overall CI conclusion for a branch (normalized) plus recent run rows."""

    overall: str
    runs: tuple[CiRun, ...] = ()


@dataclass(frozen=True)
class PullRequest:
    """A pull request, forge-neutral. ``number`` is tea's ``index`` / gh's
    ``number``; ``ci`` is the normalized overall commit-status conclusion."""

    number: int
    title: str
    state: str
    head: str
    base: str
    mergeable: bool
    url: str
    ci: str
    author: str = ""
    updated: str = ""
```

`src/marim_harness/forge/backend.py`:
```python
"""The ForgeBackend seam: the interface every concrete forge CLI satisfies.

A Protocol (not a base class) — a backend just needs these five async methods
returning neutral models. Adding a backend (e.g. gh) changes nothing else in the
system: not the models, not the tools, not the wiring."""

from __future__ import annotations

from typing import Protocol

from .models import CiStatus, PullRequest


class ForgeBackend(Protocol):
    async def list_prs(self, state: str, limit: int) -> list[PullRequest]: ...

    async def view_pr(self, number: int | None, branch: str | None) -> PullRequest | None: ...

    async def ci_status(self, branch: str) -> CiStatus: ...

    async def create_pr(
        self, title: str, body: str, base: str | None, draft: bool, head: str
    ) -> PullRequest: ...

    async def checkout_pr(self, number: int, create_branch: bool) -> str: ...
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest --no-cov tests/test_forge_models.py -v`
Expected: PASS (4 tests).

- [ ] **Step 5: Commit**

```bash
git add src/marim_harness/forge/__init__.py src/marim_harness/forge/models.py \
        src/marim_harness/forge/backend.py tests/test_forge_models.py
git commit -m "feat(forge): neutral models + ForgeBackend protocol"
```

---

### Task 2: TeaBackend pure helpers (argv builders + JSON mappers)

**Files:**
- Create: `src/marim_harness/forge/tea_backend.py` (pure helpers only in this task)
- Test: `tests/test_forge_tea_backend.py`

**Interfaces:**
- Consumes: `PullRequest`, `CiRun`, `normalize_ci`, `ForgeError` (Task 1).
- Produces (module-level, pure):
  - `PR_FIELDS: str`
  - `_list_prs_args(state: str, limit: int) -> list[str]`
  - `_create_pr_args(title, body, base, draft, head) -> list[str]`
  - `_checkout_pr_args(number: int, create_branch: bool) -> list[str]`
  - `_runs_args() -> list[str]`
  - `_map_pr(obj: dict) -> PullRequest`
  - `_map_run(obj: dict) -> CiRun`
  - `_loads(raw: str) -> object` (raises `ForgeError` on bad JSON)

- [ ] **Step 1: Write the failing test**

`tests/test_forge_tea_backend.py` (fixtures are captured verbatim from real `tea … -o json`):
```python
import pytest

from marim_harness.forge.models import ForgeError
from marim_harness.forge import tea_backend as tb

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
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest --no-cov tests/test_forge_tea_backend.py -v`
Expected: FAIL with `ModuleNotFoundError` / `AttributeError` on `tea_backend`.

- [ ] **Step 3: Write minimal implementation**

`src/marim_harness/forge/tea_backend.py` (pure helpers only for now):
```python
"""The tea (Gitea CLI) ForgeBackend. This task adds only the *pure* pieces:
argv builders, tea-JSON->neutral-model mappers, and a JSON loader. The
subprocess I/O and the TeaBackend class arrive in the next task.

All values from ``tea … -o json --fields`` arrive as strings (``index:"51"``,
``mergeable:"false"``, ``ci:"success"``); the mappers coerce them.
"""

from __future__ import annotations

import json
from typing import Any

from .models import CiRun, ForgeError, PullRequest, normalize_ci

# The one field-rich PR endpoint that also carries `ci` and `mergeable`;
# `tea pr <n>` has a different, ci-less shape and is deliberately not used.
PR_FIELDS = "index,title,state,author,head,base,mergeable,url,updated,ci"


def _list_prs_args(state: str, limit: int) -> list[str]:
    return ["pr", "list", "--state", state, "--limit", str(limit),
            "-o", "json", "--fields", PR_FIELDS]


def _create_pr_args(title: str, body: str, base: str | None, draft: bool, head: str) -> list[str]:
    args = ["pr", "create", "--head", head, "--title", title, "--description", body]
    if base:
        args += ["--base", base]
    if draft:
        args.append("--draft")
    return args


def _checkout_pr_args(number: int, create_branch: bool) -> list[str]:
    args = ["pr", "checkout", str(number)]
    if create_branch:
        args.append("-b")
    return args


def _runs_args() -> list[str]:
    return ["actions", "runs", "-o", "json"]


def _map_pr(obj: dict[str, Any]) -> PullRequest:
    return PullRequest(
        number=int(obj["index"]),
        title=obj.get("title", ""),
        state=obj.get("state", ""),
        author=obj.get("author", ""),
        head=obj.get("head", ""),
        base=obj.get("base", ""),
        mergeable=str(obj.get("mergeable", "")).strip().lower() == "true",
        url=obj.get("url", ""),
        updated=obj.get("updated", ""),
        ci=normalize_ci(obj.get("ci")),
    )


def _map_run(obj: dict[str, Any]) -> CiRun:
    return CiRun(
        workflow=obj.get("workflow", ""),
        status=obj.get("status", ""),
        event=obj.get("event", ""),
        branch=obj.get("branch", ""),
        started=obj.get("started", ""),
    )


def _loads(raw: str) -> Any:
    try:
        return json.loads(raw)
    except json.JSONDecodeError as exc:
        first = raw.strip().splitlines()[0] if raw.strip() else "<empty>"
        raise ForgeError(f"could not parse tea output: {first!r}") from exc
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest --no-cov tests/test_forge_tea_backend.py -v`
Expected: PASS (6 tests).

- [ ] **Step 5: Commit**

```bash
git add src/marim_harness/forge/tea_backend.py tests/test_forge_tea_backend.py
git commit -m "feat(forge): tea argv builders + JSON->model mappers"
```

---

### Task 3: TeaBackend I/O — `_run_tea`, `tea_available`, backend methods

**Files:**
- Modify: `src/marim_harness/forge/tea_backend.py`
- Test: `tests/test_forge_tea_backend.py` (append)

**Interfaces:**
- Consumes: pure helpers from Task 2; `CiStatus`, `PullRequest`, `ForgeError`.
- Produces:
  - `async def _run_tea(args: list[str], cwd: Path, timeout: float = 20.0) -> str` (raises `ForgeError` on launch failure / timeout / non-zero exit).
  - `def tea_available() -> bool` (tea on PATH + a tea config file exists).
  - `class TeaBackend` (root: Path) satisfying `ForgeBackend` — the five async methods.

- [ ] **Step 1: Write the failing test**

Append to `tests/test_forge_tea_backend.py`:
```python
from pathlib import Path


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
```

Add the anyio backend fixture at the top of the file (if not already present in the suite's conftest):
```python
@pytest.fixture
def anyio_backend():
    return "asyncio"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest --no-cov tests/test_forge_tea_backend.py -v`
Expected: FAIL — `AttributeError: module ... has no attribute '_run_tea'` / `tea_available` / `TeaBackend`.

- [ ] **Step 3: Write minimal implementation**

Add imports and code to `src/marim_harness/forge/tea_backend.py`:
```python
import asyncio
import contextlib
import os
import shutil
from pathlib import Path

from .models import CiStatus  # add to existing model imports
```
```python
async def _run_tea(args: list[str], cwd: Path, timeout: float = 20.0) -> str:
    """Run ``tea <args>`` as an argv list (never a shell string — user values
    like a PR body are inert) in ``cwd``. Returns stdout; raises ForgeError on
    launch failure, timeout, or non-zero exit (message = tea's stderr)."""
    try:
        proc = await asyncio.create_subprocess_exec(
            "tea", *args, cwd=str(cwd),
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
        )
    except (FileNotFoundError, OSError) as exc:
        raise ForgeError(f"could not launch tea: {exc}") from exc
    try:
        out, err = await asyncio.wait_for(proc.communicate(), timeout=timeout)
    except asyncio.TimeoutError as exc:
        with contextlib.suppress(ProcessLookupError):
            proc.kill()
        with contextlib.suppress(Exception):
            await proc.wait()
        raise ForgeError(f"tea timed out after {timeout}s") from exc
    if proc.returncode != 0:
        msg = err.decode("utf-8", "replace").strip() or f"tea exited {proc.returncode}"
        raise ForgeError(msg)
    return out.decode("utf-8", "replace")


def tea_available() -> bool:
    """True when ``tea`` is on PATH and a tea config file exists (a login is
    configured). Checked once at build time; the toolset attaches only if True."""
    if shutil.which("tea") is None:
        return False
    base = os.environ.get("XDG_CONFIG_HOME") or os.path.expanduser("~/.config")
    return (Path(base) / "tea" / "config.yml").is_file()


class TeaBackend:
    """ForgeBackend backed by the tea CLI, rooted at a workspace directory."""

    def __init__(self, root: Path) -> None:
        self._root = root

    async def list_prs(self, state: str, limit: int) -> list[PullRequest]:
        raw = await _run_tea(_list_prs_args(state, limit), self._root)
        return [_map_pr(o) for o in _loads(raw)]

    async def view_pr(self, number: int | None, branch: str | None) -> PullRequest | None:
        for pr in await self.list_prs("all", 50):
            if number is not None and pr.number == number:
                return pr
            if number is None and branch and pr.head == branch:
                return pr
        return None

    async def ci_status(self, branch: str) -> CiStatus:
        pr = next((p for p in await self.list_prs("all", 50) if p.head == branch), None)
        overall = pr.ci if pr else "unknown"
        raw = await _run_tea(_runs_args(), self._root)
        runs = tuple(_map_run(o) for o in _loads(raw) if o.get("branch") == branch)
        return CiStatus(overall=overall, runs=runs)

    async def create_pr(
        self, title: str, body: str, base: str | None, draft: bool, head: str
    ) -> PullRequest:
        # tea pr create prints text, not JSON; ignore its stdout and re-fetch by
        # head branch so the returned PullRequest has the same shape as list_prs.
        await _run_tea(_create_pr_args(title, body, base, draft, head), self._root)
        pr = await self.view_pr(None, head)
        if pr is None:
            raise ForgeError("PR created but could not be re-fetched by head branch")
        return pr

    async def checkout_pr(self, number: int, create_branch: bool) -> str:
        await _run_tea(_checkout_pr_args(number, create_branch), self._root)
        return f"Checked out PR #{number}."
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest --no-cov tests/test_forge_tea_backend.py -v`
Expected: PASS (all tests). Then `uv run pyright src/marim_harness/forge/tea_backend.py` clean (TeaBackend structurally satisfies ForgeBackend).

- [ ] **Step 5: Commit**

```bash
git add src/marim_harness/forge/tea_backend.py tests/test_forge_tea_backend.py
git commit -m "feat(forge): _run_tea choke point, tea_available, TeaBackend methods"
```

---

### Task 4: git helpers (`gitref.py`)

**Files:**
- Create: `src/marim_harness/forge/gitref.py`
- Test: `tests/test_forge_gitref.py`

**Interfaces:**
- Produces:
  - `async def current_branch(root: Path) -> str | None` — the checked-out branch, or None if detached/unavailable.
  - `async def branch_pushed(root: Path, branch: str) -> bool` — True if `refs/remotes/origin/<branch>` exists locally (no network).

- [ ] **Step 1: Write the failing test**

`tests/test_forge_gitref.py`:
```python
from pathlib import Path

import pytest

from marim_harness.forge import gitref


@pytest.fixture
def anyio_backend():
    return "asyncio"


def _patch_git(monkeypatch, out: str | None):
    async def fake_git(args, root):
        return out
    monkeypatch.setattr(gitref, "_git", fake_git)


@pytest.mark.anyio
async def test_current_branch_returns_name(monkeypatch):
    _patch_git(monkeypatch, "feature/x\n")
    assert await gitref.current_branch(Path(".")) == "feature/x"


@pytest.mark.anyio
async def test_current_branch_none_on_detached(monkeypatch):
    _patch_git(monkeypatch, "HEAD\n")
    assert await gitref.current_branch(Path(".")) is None


@pytest.mark.anyio
async def test_current_branch_none_on_failure(monkeypatch):
    _patch_git(monkeypatch, None)
    assert await gitref.current_branch(Path(".")) is None


@pytest.mark.anyio
async def test_branch_pushed_true_when_ref_present(monkeypatch):
    _patch_git(monkeypatch, "abc123 refs/remotes/origin/feature/x\n")
    assert await gitref.branch_pushed(Path("."), "feature/x") is True


@pytest.mark.anyio
async def test_branch_pushed_false_when_absent(monkeypatch):
    _patch_git(monkeypatch, None)
    assert await gitref.branch_pushed(Path("."), "feature/x") is False
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest --no-cov tests/test_forge_gitref.py -v`
Expected: FAIL — `ModuleNotFoundError: ... forge.gitref`.

- [ ] **Step 3: Write minimal implementation**

`src/marim_harness/forge/gitref.py`:
```python
"""Small git helpers used by the forge tools for branch resolution and the
create_pr preflight. Forge-neutral (pure git), so they are shared across every
backend. All subprocess access goes through ``_git`` for easy stubbing."""

from __future__ import annotations

import asyncio
import contextlib
from pathlib import Path


async def _git(args: list[str], root: Path) -> str | None:
    """Run ``git <args>`` in ``root``; return stdout, or None on any failure
    (missing git, non-zero exit). Best-effort — never raises into a tool."""
    try:
        proc = await asyncio.create_subprocess_exec(
            "git", *args, cwd=str(root),
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL,
        )
    except (FileNotFoundError, OSError):
        return None
    try:
        out, _ = await asyncio.wait_for(proc.communicate(), timeout=10.0)
    except asyncio.TimeoutError:
        with contextlib.suppress(ProcessLookupError):
            proc.kill()
        with contextlib.suppress(Exception):
            await proc.wait()
        return None
    if proc.returncode != 0:
        return None
    return out.decode("utf-8", "replace")


async def current_branch(root: Path) -> str | None:
    """The checked-out branch name, or None when detached / not a repo."""
    raw = await _git(["rev-parse", "--abbrev-ref", "HEAD"], root)
    if raw is None:
        return None
    branch = raw.strip()
    return None if branch in ("", "HEAD") else branch


async def branch_pushed(root: Path, branch: str) -> bool:
    """True if the local remote-tracking ref ``origin/<branch>`` exists — i.e.
    the branch has been pushed (as of the last fetch/push). No network."""
    raw = await _git(
        ["rev-parse", "--verify", "--quiet", f"refs/remotes/origin/{branch}"], root
    )
    return bool(raw and raw.strip())
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest --no-cov tests/test_forge_gitref.py -v`
Expected: PASS (5 tests).

- [ ] **Step 5: Commit**

```bash
git add src/marim_harness/forge/gitref.py tests/test_forge_gitref.py
git commit -m "feat(forge): git branch-resolution and pushed-check helpers"
```

---

### Task 5: `select_backend`

**Files:**
- Create: `src/marim_harness/forge/select.py`
- Test: `tests/test_forge_select.py`

**Interfaces:**
- Consumes: `TeaBackend`, `tea_available` (Task 3); `ForgeBackend` (Task 1).
- Produces: `def select_backend(forge_enabled: bool, root: Path) -> ForgeBackend | None` — the single build-time availability decision (config flag AND tea availability). v1 returns a `TeaBackend` or None; a future gh branch is added here.

- [ ] **Step 1: Write the failing test**

`tests/test_forge_select.py`:
```python
from pathlib import Path

from marim_harness.forge import select as sel
from marim_harness.forge.tea_backend import TeaBackend


def test_disabled_returns_none(monkeypatch):
    monkeypatch.setattr(sel, "tea_available", lambda: True)
    assert sel.select_backend(False, Path(".")) is None


def test_enabled_but_tea_unavailable_returns_none(monkeypatch):
    monkeypatch.setattr(sel, "tea_available", lambda: False)
    assert sel.select_backend(True, Path(".")) is None


def test_enabled_and_available_returns_tea_backend(monkeypatch):
    monkeypatch.setattr(sel, "tea_available", lambda: True)
    backend = sel.select_backend(True, Path("/repo"))
    assert isinstance(backend, TeaBackend)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest --no-cov tests/test_forge_select.py -v`
Expected: FAIL — `ModuleNotFoundError: ... forge.select`.

- [ ] **Step 3: Write minimal implementation**

`src/marim_harness/forge/select.py`:
```python
"""The single build-time forge-backend decision. Folds the config flag and CLI
availability into one place. Adding a gh backend means one more branch here
(e.g. choose by the origin remote host or which CLI is authenticated)."""

from __future__ import annotations

from pathlib import Path

from .backend import ForgeBackend
from .tea_backend import TeaBackend, tea_available


def select_backend(forge_enabled: bool, root: Path) -> ForgeBackend | None:
    """Return the forge backend to use, or None to attach no forge toolset.

    v1: tea only. Returns None when forge is disabled or tea is unavailable."""
    if not forge_enabled:
        return None
    if tea_available():
        return TeaBackend(root)
    return None
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest --no-cov tests/test_forge_select.py -v`
Expected: PASS (3 tests).

- [ ] **Step 5: Commit**

```bash
git add src/marim_harness/forge/select.py tests/test_forge_select.py
git commit -m "feat(forge): select_backend build-time gate (tea v1)"
```

---

### Task 6: Forge tools + toolset (`tools/forge_tools.py`)

**Files:**
- Create: `src/marim_harness/tools/forge_tools.py`
- Test: `tests/test_forge_tools.py`

**Interfaces:**
- Consumes: `ForgeBackend`, `ForgeError`, `PullRequest`, `CiStatus`, `CiRun` (Tasks 1); `current_branch`, `branch_pushed` (Task 4); `select_backend` (Task 5); `Deps` (`runtime.deps`); `FunctionToolset`, `RunContext` (pydantic_ai).
- Produces:
  - `def build_forge_toolset(backend: ForgeBackend) -> FunctionToolset[Deps]` — the 5 tools closing over `backend`; `create_pr`/`checkout_pr` gated (`requires_approval=True`).
  - `def forge_toolsets(forge_enabled: bool, root: Path) -> list[FunctionToolset[Deps]]` — `[build_forge_toolset(select_backend(...))]` or `[]`. (Lives here, not in `forge/`, so `forge/` never imports `tools`.)

- [ ] **Step 1: Write the failing test**

`tests/test_forge_tools.py`:
```python
from dataclasses import dataclass
from pathlib import Path

import pytest

from marim_harness.forge.models import CiRun, CiStatus, ForgeError, PullRequest
from marim_harness.tools import forge_tools as ft


@pytest.fixture
def anyio_backend():
    return "asyncio"


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
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest --no-cov tests/test_forge_tools.py -v`
Expected: FAIL — `ModuleNotFoundError: ... tools.forge_tools`.

- [ ] **Step 3: Write minimal implementation**

`src/marim_harness/tools/forge_tools.py`:
```python
"""Forge-agnostic PR/CI tools + toolset assembly.

Each tool closes over the selected ForgeBackend (bound at build time), so the
tool bodies never mention tea/gh. Read tools are ungated; create_pr/checkout_pr
gate for approval (create/checkout mutate remote/working-tree state — the same
boundary as net_tools/bash). ``forge_toolsets`` lives here rather than in the
``forge`` package so ``forge`` never imports the tools layer."""

from __future__ import annotations

from pathlib import Path

from pydantic_ai import RunContext
from pydantic_ai.toolsets import FunctionToolset

from ..forge.backend import ForgeBackend
from ..forge.gitref import branch_pushed, current_branch
from ..forge.models import ForgeError
from ..forge.select import select_backend
from ..runtime.deps import Deps


def build_forge_toolset(backend: ForgeBackend) -> FunctionToolset[Deps]:
    ts: FunctionToolset[Deps] = FunctionToolset()

    async def list_prs(ctx: RunContext[Deps], state: str = "open", limit: int = 30) -> str:
        """List pull requests. `state` is open|closed|all (default open). Returns
        one line per PR: number, state, title, and overall CI conclusion."""
        try:
            prs = await backend.list_prs(state, limit)
        except ForgeError as exc:
            return f"Forge error: {exc}"
        if not prs:
            return f"No {state} pull requests."
        return "\n".join(f"#{p.number} [{p.state}] {p.title} (ci: {p.ci})" for p in prs)

    async def view_pr(ctx: RunContext[Deps], number: int | None = None) -> str:
        """Show one pull request. With no `number`, resolves the PR for the
        current branch. Reports head→base, mergeability, CI conclusion, and URL."""
        branch = None
        if number is None:
            branch = await current_branch(ctx.deps.workspace.root)
            if branch is None:
                return "Not on a branch — pass a PR number."
        try:
            pr = await backend.view_pr(number, branch)
        except ForgeError as exc:
            return f"Forge error: {exc}"
        if pr is None:
            what = f"#{number}" if number is not None else f"branch '{branch}'"
            return f"No PR found for {what}."
        return (f"#{pr.number} [{pr.state}] {pr.title}\n"
                f"{pr.head} → {pr.base}\n"
                f"mergeable: {pr.mergeable} | ci: {pr.ci}\n{pr.url}")

    async def ci_status(
        ctx: RunContext[Deps], branch: str | None = None, pr: int | None = None
    ) -> str:
        """Report CI for a branch (defaults to the current branch). Shows the
        overall conclusion plus recent workflow runs (most recent first)."""
        b = branch or await current_branch(ctx.deps.workspace.root)
        if b is None:
            return "Not on a branch — pass branch=."
        try:
            st = await backend.ci_status(b)
        except ForgeError as exc:
            return f"Forge error: {exc}"
        lines = [f"CI for {b}: {st.overall}"]
        lines += [f"  {r.workflow} [{r.status}] ({r.event} {r.started})" for r in st.runs[:10]]
        return "\n".join(lines)

    async def create_pr(
        ctx: RunContext[Deps], title: str, body: str = "", base: str | None = None,
        draft: bool = False,
    ) -> str:
        """Open a pull request from the current branch. Requires the branch to be
        pushed first (it will not push for you) and refuses if a PR already
        exists for it. `base` defaults to the repo's default branch."""
        root = ctx.deps.workspace.root
        branch = await current_branch(root)
        if branch is None:
            return "Not on a branch — cannot open a PR."
        if not await branch_pushed(root, branch):
            return f"Branch '{branch}' is not pushed. Run: git push -u origin {branch}"
        try:
            existing = await backend.view_pr(None, branch)
            if existing is not None:
                return f"A PR already exists for '{branch}': #{existing.number} {existing.url}"
            pr = await backend.create_pr(title, body, base, draft, branch)
        except ForgeError as exc:
            return f"Forge error: {exc}"
        return f"Created PR #{pr.number}: {pr.url}"

    async def checkout_pr(
        ctx: RunContext[Deps], number: int, create_branch: bool = True
    ) -> str:
        """Check out a pull request locally (fetches and switches the working
        tree). `create_branch` makes a local branch if one doesn't exist yet."""
        try:
            return await backend.checkout_pr(number, create_branch)
        except ForgeError as exc:
            return f"Forge error: {exc}"

    ts.add_function(list_prs)
    ts.add_function(view_pr)
    ts.add_function(ci_status)
    ts.add_function(create_pr, requires_approval=True)
    ts.add_function(checkout_pr, requires_approval=True)
    return ts


def forge_toolsets(forge_enabled: bool, root: Path) -> list[FunctionToolset[Deps]]:
    """The forge toolsets to attach to the Agent: a single-element list when a
    backend is selected, else empty. This is the one wiring seam build_harness
    calls."""
    backend = select_backend(forge_enabled, root)
    return [build_forge_toolset(backend)] if backend else []
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest --no-cov tests/test_forge_tools.py -v`
Expected: PASS (all tests).

- [ ] **Step 5: Commit**

```bash
git add src/marim_harness/tools/forge_tools.py tests/test_forge_tools.py
git commit -m "feat(forge): forge-agnostic PR/CI tools + toolset assembly"
```

---

### Task 7: Wire into config, Agent construction, and docs

**Files:**
- Modify: `src/marim_harness/config/model.py` (add field + env read)
- Modify: `src/marim_harness/runtime/harness.py` (`HarnessConfig.forge_enabled`; `toolsets=` in `build_collaborators`)
- Modify: `src/marim_harness/runtime/bootstrap.py` (pass `forge_enabled`)
- Modify: `CLAUDE.md`, `.env.example`
- Test: `tests/test_forge_wiring.py`

**Interfaces:**
- Consumes: `forge_toolsets` (Task 6); the config dataclass and `_bool_env` in `config/model.py`; `HarnessConfig` and `build_collaborators` in `runtime/harness.py`.
- Produces: `MARIM_FORGE` env flag → `cfg.forge_enabled` → `HarnessConfig.forge_enabled` → the Agent's attached forge toolset.

- [ ] **Step 1: Write the failing test**

`tests/test_forge_wiring.py`:
```python
from pathlib import Path

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
```

> Config facts (confirmed): the loader is `load_config()` (re-exported from `marim_harness.config`), returning a `ModelConfig` dataclass. The `forge_enabled` field goes on `ModelConfig` (alongside `lsp_enabled`); the env read goes in `_common_kwargs()`.

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest --no-cov tests/test_forge_wiring.py -v`
Expected: FAIL — `AttributeError: ... has no attribute 'forge_enabled'` (config field absent).

- [ ] **Step 3: Write minimal implementation**

In `src/marim_harness/config/model.py`, add the field to the `ModelConfig` dataclass next to `lsp_enabled`:
```python
    forge_enabled: bool = True
```
and in `_common_kwargs()` (next to `lsp_enabled=_bool_env("MARIM_LSP", True)`):
```python
        forge_enabled=_bool_env("MARIM_FORGE", True),
```

In `src/marim_harness/runtime/harness.py`, add to `HarnessConfig` (next to `lsp_enabled: bool = True`):
```python
    forge_enabled: bool = True
```
Then in `build_collaborators`, before `agent = Agent(`, add the import at top of file:
```python
from ..tools.forge_tools import forge_toolsets
```
and compute + pass the toolsets into the `Agent(...)` constructor:
```python
    forge_ts = forge_toolsets(cfg.forge_enabled, deps.workspace.root)
    agent = Agent(
        model,
        deps_type=Deps,
        instructions=instructions,
        output_type=[str, DeferredToolRequests],
        retries=2,
        model_settings=_DEFAULT_MODEL_SETTINGS,
        toolsets=forge_ts,
        capabilities=[
            ProcessHistory(_drop_nameless_tool_calls),
            ProcessHistory(suggest_unknown_tool_retry),
            DiscoveredInstructionsCapability(mcp),
        ],
    )
```
(Only `toolsets=forge_ts` is new; keep the existing args verbatim.)

In `src/marim_harness/runtime/bootstrap.py`, add to the `HarnessConfig(...)` call (next to `lsp_enabled=cfg.lsp_enabled,`):
```python
            forge_enabled=cfg.forge_enabled,
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest --no-cov tests/test_forge_wiring.py -v`
Expected: PASS (3 tests).

- [ ] **Step 5: Update docs**

In `CLAUDE.md`, under the supporting-subsystems list (near the `lsp/` bullet), add:
```markdown
- `forge/` — Gitea/GitHub integration via a `ForgeBackend` seam. `TeaBackend`
  shells out to the `tea` CLI (`--output json`); five forge-agnostic tools
  (`tools/forge_tools.py`) list/view PRs, check CI, and open/check out PRs, with
  create/checkout gated for approval. Attached at build time only when
  `MARIM_FORGE` is on (default) and a backend is available (`tea` on PATH + a
  configured login). A `gh` backend is a future drop-in behind the same protocol.
```
In `.env.example`, add near the other `MARIM_*` toggles:
```
# Forge (Gitea/GitHub) tools via the tea CLI; on by default. Set to 0 to disable.
# MARIM_FORGE=1
```

- [ ] **Step 6: Full verification + commit**

Run the CI order:
```bash
uv run ruff check src tests
uv run pyright
uv run pytest
```
Expected: all clean/green.

```bash
git add src/marim_harness/config/model.py src/marim_harness/runtime/harness.py \
        src/marim_harness/runtime/bootstrap.py CLAUDE.md .env.example tests/test_forge_wiring.py
git commit -m "feat(forge): wire forge toolset into config, Agent, and docs"
```

---

## Manual verification (after Task 7)

Drive the real tool end-to-end once (uses the live tea login on this machine):
```bash
uv run python -c "
import asyncio
from pathlib import Path
from marim_harness.tools.forge_tools import build_forge_toolset
from marim_harness.forge.select import select_backend

async def main():
    backend = select_backend(True, Path('.'))
    print('backend:', type(backend).__name__ if backend else None)
    prs = await backend.list_prs('all', 3)
    for p in prs:
        print(p.number, p.state, p.ci, p.title[:40])
    st = await backend.ci_status('master')
    print('CI(master) overall:', st.overall, '| runs:', len(st.runs))

asyncio.run(main())
"
```
Expected: prints the tea backend, a few recent PRs with normalized `ci`, and the master CI overall — confirming argv/JSON mapping against live tea. (Do NOT run `create_pr` as a smoke — it would open a real PR.)

## Self-Review

**Spec coverage:**
- forge/ module layout (models/backend/tea_backend/select + tools/forge_tools) → Tasks 1–6. ✓
- Neutral models incl. resolved CI grounding (index→int, mergeable→bool, ci normalized; CiRun.conclusion/url None on tea; overall from PR ci) → Task 1 + Task 3 `ci_status`. ✓
- ForgeBackend protocol seam → Task 1. ✓
- Five tools with exact gating (reads ungated; create/checkout `requires_approval=True`) → Task 6 `test_toolset_gating_flags`. ✓
- create_pr preflight (unpushed → instruction, existing PR → refusal, no auto-push) → Task 6 tests. ✓
- Branch-default resolution for view_pr/ci_status → Task 6 tests. ✓
- Single build-time availability decision (flag + tea_available) → Task 5 + Task 7. ✓
- _run_tea argv-list injection guard + timeout + stderr→ForgeError + JSON-drift ForgeError → Tasks 2/3 tests. ✓
- Not granted to sub-agents → satisfied by construction (registered only via the Agent's `toolsets=`, never in `provider.register_subagent` / `_SUBAGENT_FNS`); no task adds it. ✓
- Config surface `forge_enabled` / `MARIM_FORGE` default on → Task 7 tests. ✓
- Future gh path unchanged-seam → documented; no code. ✓

**Placeholder scan:** No TBD/TODO; every code step shows complete code. The one flagged uncertainty (config loader entrypoint name) is called out explicitly with the field name fixed. ✓

**Type consistency:** `PullRequest`/`CiRun`/`CiStatus` fields identical across Tasks 1/2/3/6; `create_pr(..., head)` 5-arg signature consistent in backend (Task 1 protocol, Task 3 TeaBackend) and its caller (Task 6 tool passes current branch as head); `forge_toolsets`/`select_backend`/`build_forge_toolset` names consistent across Tasks 5–7. ✓
