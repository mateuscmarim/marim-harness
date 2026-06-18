# Git Worktree Workflow (Sub-project A) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Let a user run a marim session inside a git worktree via a `--worktree <branch>` launch flag, and manage worktrees from within a session via a `/worktree` command.

**Architecture:** A new UI-agnostic `workspace/worktree.py` module owns all `git worktree` plumbing (create/list/remove under `<repo>/.worktrees/<branch>`). The CLI flag resolves the effective workspace to a worktree path *before* `build_harness`, so `Deps.workspace_root` stays immutable and everything downstream (sessions, tools, LSP, MCP) scopes automatically. The `/worktree` command creates/lists/removes worktrees but never switches the live session — it prints a launch hint. File-traversal tools are taught to skip `.worktrees/`.

**Tech Stack:** Python 3.10+, `subprocess` (list argv, never `shell=True`), `argparse`, pytest against a real temporary git repo, Textual TUI command framework.

## Global Constraints

- All git calls use `subprocess.run([...], cwd=repo_root, capture_output=True, text=True)` with a **list argv** — never `shell=True`.
- On non-zero git exit, raise `WorktreeError(result.stderr.strip() or result.stdout.strip())`.
- `workspace/worktree.py` imports **no** Textual, agent, or `Deps` code — pure functions + one dataclass + one exception.
- The worktree directory is always `repo_root / ".worktrees" / branch` (`WORKTREES_DIRNAME = ".worktrees"`). No config override (YAGNI).
- Base for a *new* branch is the repo's current HEAD — plain `git worktree add -b <branch> <path>`; no base-detection logic.
- `create_or_reuse_worktree` is idempotent: an existing worktree for the branch returns its path without re-adding.
- Branch validation rejects empty, leading `-`, leading `/`, and any `.`/`..`/empty path segment; slashes are otherwise allowed (`feat/x`).
- `/worktree create` does **not** switch the running session; it posts a `marim --worktree <branch>` launch hint.
- Gates (must be green before each commit): `uv run ruff check src tests`, `uv run pyright src`, `uv run pytest`.

---

### Task 1: `workspace/worktree.py` git module

**Files:**
- Create: `src/marim_harness/workspace/worktree.py`
- Test: `tests/test_worktree.py`

**Interfaces:**
- Consumes: nothing (leaf module; only stdlib + `git` on PATH).
- Produces (later tasks depend on these exact signatures):
  - `WORKTREES_DIRNAME: str = ".worktrees"`
  - `class WorktreeError(Exception)`
  - `@dataclass(frozen=True) class WorktreeInfo` with fields `path: Path`, `branch: str`, `head: str`, `is_current: bool`
  - `repo_root(path: Path) -> Path | None`
  - `list_worktrees(repo_root: Path, current: Path | None = None) -> list[WorktreeInfo]`
  - `create_or_reuse_worktree(repo_root: Path, branch: str) -> Path`
  - `remove_worktree(repo_root: Path, branch: str) -> None`

- [ ] **Step 1: Write the failing tests**

Create `tests/test_worktree.py`:

```python
import subprocess
from pathlib import Path

import pytest

from marim_harness.workspace import worktree as wt


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    """A real git repo with one commit, on branch `main`."""
    subprocess.run(["git", "init", "-q", "-b", "main"], cwd=tmp_path, check=True)
    subprocess.run(["git", "config", "user.email", "t@example.com"], cwd=tmp_path, check=True)
    subprocess.run(["git", "config", "user.name", "Test"], cwd=tmp_path, check=True)
    (tmp_path / "README.md").write_text("hi\n")
    subprocess.run(["git", "add", "."], cwd=tmp_path, check=True)
    subprocess.run(["git", "commit", "-qm", "init"], cwd=tmp_path, check=True)
    return tmp_path


def test_repo_root_inside_repo(repo: Path):
    assert wt.repo_root(repo) == repo.resolve()
    sub = repo / "sub"
    sub.mkdir()
    assert wt.repo_root(sub) == repo.resolve()


def test_repo_root_outside_repo(tmp_path: Path):
    assert wt.repo_root(tmp_path) is None


def test_repo_root_missing_dir(tmp_path: Path):
    assert wt.repo_root(tmp_path / "does-not-exist") is None


def test_create_new_branch_worktree(repo: Path):
    path = wt.create_or_reuse_worktree(repo, "feat/x")
    assert path == repo / ".worktrees" / "feat/x"
    assert (path / ".git").exists()  # a real checkout
    assert (path / "README.md").read_text() == "hi\n"
    # the branch now exists
    rc = subprocess.run(
        ["git", "show-ref", "--verify", "--quiet", "refs/heads/feat/x"], cwd=repo
    ).returncode
    assert rc == 0


def test_create_is_idempotent(repo: Path):
    p1 = wt.create_or_reuse_worktree(repo, "feat/x")
    p2 = wt.create_or_reuse_worktree(repo, "feat/x")
    assert p1 == p2


def test_create_reuses_existing_branch(repo: Path):
    subprocess.run(["git", "branch", "existing"], cwd=repo, check=True)
    path = wt.create_or_reuse_worktree(repo, "existing")
    assert path == repo / ".worktrees" / "existing"
    # HEAD of the worktree is the `existing` branch
    out = subprocess.run(
        ["git", "rev-parse", "--abbrev-ref", "HEAD"],
        cwd=path, capture_output=True, text=True, check=True,
    ).stdout.strip()
    assert out == "existing"


def test_list_worktrees_and_is_current(repo: Path):
    wt.create_or_reuse_worktree(repo, "feat/x")
    rows = wt.list_worktrees(repo, current=repo)
    branches = {r.branch for r in rows}
    assert "main" in branches
    assert "feat/x" in branches
    main_row = next(r for r in rows if r.branch == "main")
    assert main_row.is_current is True
    feat_row = next(r for r in rows if r.branch == "feat/x")
    assert feat_row.is_current is False
    assert feat_row.head  # a sha was parsed


def test_remove_worktree_keeps_branch(repo: Path):
    path = wt.create_or_reuse_worktree(repo, "feat/x")
    wt.remove_worktree(repo, "feat/x")
    assert not path.exists()
    rc = subprocess.run(
        ["git", "show-ref", "--verify", "--quiet", "refs/heads/feat/x"], cwd=repo
    ).returncode
    assert rc == 0  # branch survives


def test_remove_refuses_dirty_worktree(repo: Path):
    path = wt.create_or_reuse_worktree(repo, "feat/x")
    (path / "dirty.txt").write_text("uncommitted\n")
    with pytest.raises(wt.WorktreeError):
        wt.remove_worktree(repo, "feat/x")


@pytest.mark.parametrize("bad", ["", "-x", "../escape", "/abs", "feat/", "a/../b"])
def test_validate_rejects_bad_branches(repo: Path, bad: str):
    with pytest.raises(wt.WorktreeError):
        wt.create_or_reuse_worktree(repo, bad)


def test_validate_allows_slashes(repo: Path):
    path = wt.create_or_reuse_worktree(repo, "feat/nested/x")
    assert path == repo / ".worktrees" / "feat/nested/x"
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `uv run pytest tests/test_worktree.py -q`
Expected: FAIL — `ModuleNotFoundError: No module named 'marim_harness.workspace.worktree'`.

- [ ] **Step 3: Write the implementation**

Create `src/marim_harness/workspace/worktree.py`:

```python
"""Git worktree operations: create/list/remove worktrees under <repo>/.worktrees.

UI-agnostic. The only module that shells out to ``git`` for worktree management.
Every function takes an already-resolved repo root (see ``repo_root``). Git
failures surface as ``WorktreeError`` carrying git's stderr; callers decide how
to present them. Nothing here imports Deps, Textual, or the agent.
"""

from __future__ import annotations

import subprocess
from dataclasses import dataclass
from pathlib import Path

WORKTREES_DIRNAME = ".worktrees"


class WorktreeError(Exception):
    """A git worktree operation failed; message carries git's stderr."""


@dataclass(frozen=True)
class WorktreeInfo:
    path: Path        # absolute worktree path
    branch: str       # branch name without refs/heads/, or "" if detached
    head: str         # commit sha
    is_current: bool  # True if path == the `current` arg passed to list_worktrees


def _git(repo_root: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", *args], cwd=repo_root, capture_output=True, text=True
    )


def _check(result: subprocess.CompletedProcess[str]) -> subprocess.CompletedProcess[str]:
    if result.returncode != 0:
        raise WorktreeError(result.stderr.strip() or result.stdout.strip())
    return result


def repo_root(path: Path) -> Path | None:
    """`git -C <path> rev-parse --show-toplevel`, or None if `path` is not in a
    git repo, does not exist, or git is not installed. Returns the **main**
    worktree's toplevel."""
    try:
        result = subprocess.run(
            ["git", "rev-parse", "--show-toplevel"],
            cwd=path, capture_output=True, text=True,
        )
    except (FileNotFoundError, NotADirectoryError):
        return None
    if result.returncode != 0:
        return None
    return Path(result.stdout.strip())


def _validate_branch(branch: str) -> None:
    """Reject empty, leading '-', leading '/', or any '.'/'..'/empty segment.
    Slashes are otherwise allowed (e.g. 'feat/x')."""
    segments = branch.split("/")
    if (
        not branch
        or branch.startswith("-")
        or any(seg in ("", ".", "..") for seg in segments)
    ):
        raise WorktreeError(f"invalid branch name: {branch!r}")


def _branch_exists(repo_root: Path, branch: str) -> bool:
    return _git(
        repo_root, "show-ref", "--verify", "--quiet", f"refs/heads/{branch}"
    ).returncode == 0


def list_worktrees(repo_root: Path, current: Path | None = None) -> list[WorktreeInfo]:
    """Parse `git worktree list --porcelain` into WorktreeInfo rows. `is_current`
    is True for the row whose path resolves to `current`."""
    result = _check(_git(repo_root, "worktree", "list", "--porcelain"))
    cur = current.resolve() if current is not None else None
    infos: list[WorktreeInfo] = []
    path: Path | None = None
    head = ""
    branch = ""

    def flush() -> None:
        nonlocal path, head, branch
        if path is not None:
            infos.append(WorktreeInfo(
                path=path,
                branch=branch,
                head=head,
                is_current=cur is not None and path.resolve() == cur,
            ))
        path, head, branch = None, "", ""

    for line in result.stdout.splitlines():
        if line.startswith("worktree "):
            flush()
            path = Path(line[len("worktree "):])
        elif line.startswith("HEAD "):
            head = line[len("HEAD "):]
        elif line.startswith("branch "):
            branch = line[len("branch "):].removeprefix("refs/heads/")
        elif line == "detached":
            branch = ""
    flush()
    return infos


def create_or_reuse_worktree(repo_root: Path, branch: str) -> Path:
    """Return the worktree path for `branch` under <repo_root>/.worktrees/<branch>.

    - If a worktree for `branch` already exists, return its path (idempotent).
    - Else if branch `branch` exists, add a worktree checking it out there.
    - Else create `branch` from current HEAD and add the worktree.
    Raises WorktreeError on validation failure or any git failure (e.g. the
    branch is already checked out in another worktree)."""
    _validate_branch(branch)
    for info in list_worktrees(repo_root):
        if info.branch == branch:
            return info.path
    target = repo_root / WORKTREES_DIRNAME / branch
    if _branch_exists(repo_root, branch):
        _check(_git(repo_root, "worktree", "add", str(target), branch))
    else:
        _check(_git(repo_root, "worktree", "add", "-b", branch, str(target)))
    return target


def remove_worktree(repo_root: Path, branch: str) -> None:
    """`git worktree remove <repo_root>/.worktrees/<branch>`. Refuses if the
    worktree is dirty or is the current one (git's own rules). Never deletes the
    branch. Raises WorktreeError on failure."""
    _validate_branch(branch)
    target = repo_root / WORKTREES_DIRNAME / branch
    _check(_git(repo_root, "worktree", "remove", str(target)))
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `uv run pytest tests/test_worktree.py -q`
Expected: PASS (12 tests).

- [ ] **Step 5: Run gates**

Run: `uv run ruff check src tests && uv run pyright src && uv run pytest -q`
Expected: all green.

- [ ] **Step 6: Commit**

```bash
git add src/marim_harness/workspace/worktree.py tests/test_worktree.py
git commit -m "feat(worktree): add git-worktree plumbing module

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>
Claude-Session: https://claude.ai/code/session_01J1DGg5LFX9aBnYM56y1j5x"
```

---

### Task 2: `--worktree` launch flag

**Files:**
- Modify: `src/marim_harness/interfaces/cli/default_cmd.py`
- Test: `tests/test_cli.py` (add to the existing file)

**Interfaces:**
- Consumes from Task 1: `repo_root(path) -> Path | None`, `create_or_reuse_worktree(repo_root, branch) -> Path`, `WorktreeError`.
- Produces: a `--worktree BRANCH` CLI flag and an internal helper `_enter_worktree(workspace: Path, branch: str, err) -> Path | None` (returns the worktree path, or `None` after printing an error to `err`).

> **Note on error handling:** the spec sketch used `parser.error(...)`, but `run_default` already uses an injectable `err` stream and the `print(msg, file=err); return 2` pattern (see `default_cmd.py:59-61`). Follow that pattern via the testable `_enter_worktree` helper so the failure path is unit-testable without `sys.exit`. The observable behavior — message to stderr, non-zero exit — matches the spec's intent.

- [ ] **Step 1: Write the failing tests**

Add to `tests/test_cli.py`:

```python
import io
import subprocess
from pathlib import Path

from marim_harness.interfaces.cli.default_cmd import _build_parser, _enter_worktree


def _git_repo(tmp_path: Path) -> Path:
    subprocess.run(["git", "init", "-q", "-b", "main"], cwd=tmp_path, check=True)
    subprocess.run(["git", "config", "user.email", "t@example.com"], cwd=tmp_path, check=True)
    subprocess.run(["git", "config", "user.name", "Test"], cwd=tmp_path, check=True)
    (tmp_path / "README.md").write_text("hi\n")
    subprocess.run(["git", "add", "."], cwd=tmp_path, check=True)
    subprocess.run(["git", "commit", "-qm", "init"], cwd=tmp_path, check=True)
    return tmp_path


def test_parser_accepts_worktree_flag():
    args = _build_parser().parse_args(["--worktree", "feat/x"])
    assert args.worktree == "feat/x"


def test_parser_worktree_defaults_none():
    args = _build_parser().parse_args([])
    assert args.worktree is None


def test_enter_worktree_resolves_path(tmp_path: Path):
    repo = _git_repo(tmp_path)
    err = io.StringIO()
    result = _enter_worktree(repo, "feat/x", err)
    assert result == repo / ".worktrees" / "feat/x"
    assert err.getvalue() == ""


def test_enter_worktree_non_git_dir_returns_none(tmp_path: Path):
    err = io.StringIO()
    result = _enter_worktree(tmp_path, "feat/x", err)
    assert result is None
    assert "not a git repository" in err.getvalue()


def test_enter_worktree_bad_branch_returns_none(tmp_path: Path):
    repo = _git_repo(tmp_path)
    err = io.StringIO()
    result = _enter_worktree(repo, "../escape", err)
    assert result is None
    assert "--worktree" in err.getvalue()
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `uv run pytest tests/test_cli.py -q -k worktree`
Expected: FAIL — `ImportError: cannot import name '_enter_worktree'` and `--worktree` unrecognized.

- [ ] **Step 3: Add the flag to the parser**

In `src/marim_harness/interfaces/cli/default_cmd.py`, inside `_build_parser()`, add after the `--mode` argument (before `return p`):

```python
    p.add_argument(
        "--worktree", metavar="BRANCH", default=None,
        help="run inside a git worktree for BRANCH under <repo>/.worktrees/, "
             "creating it (from current HEAD) or reusing it",
    )
```

- [ ] **Step 4: Add the `_enter_worktree` helper**

In the same file, add this function above `run_default`:

```python
def _enter_worktree(workspace, branch, err):
    """Resolve `workspace` to a git worktree for `branch`. Returns the worktree
    path, or None after printing an error to `err`."""
    from ...workspace.worktree import (
        WorktreeError,
        create_or_reuse_worktree,
        repo_root,
    )

    root = repo_root(workspace)
    if root is None:
        print(f"--worktree: {workspace} is not a git repository", file=err)
        return None
    try:
        return create_or_reuse_worktree(root, branch)
    except WorktreeError as exc:
        print(f"--worktree: {exc}", file=err)
        return None
```

- [ ] **Step 5: Wire the helper into `run_default`**

In `run_default`, immediately after the line `workspace = Path(args.workspace).resolve() if args.workspace else Path.cwd()` (currently `default_cmd.py:54`), insert:

```python
    if args.worktree:
        workspace = _enter_worktree(workspace, args.worktree, err)
        if workspace is None:
            return 2
```

- [ ] **Step 6: Run the tests to verify they pass**

Run: `uv run pytest tests/test_cli.py -q -k worktree`
Expected: PASS (5 tests).

- [ ] **Step 7: Run gates**

Run: `uv run ruff check src tests && uv run pyright src && uv run pytest -q`
Expected: all green.

- [ ] **Step 8: Commit**

```bash
git add src/marim_harness/interfaces/cli/default_cmd.py tests/test_cli.py
git commit -m "feat(worktree): add --worktree launch flag

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>
Claude-Session: https://claude.ai/code/session_01J1DGg5LFX9aBnYM56y1j5x"
```

---

### Task 3: `/worktree` TUI command

**Files:**
- Modify: `src/marim_harness/interfaces/tui/commands.py`
- Test: `tests/test_commands.py` (add to the existing file)

**Interfaces:**
- Consumes from Task 1: `repo_root`, `list_worktrees`, `create_or_reuse_worktree`, `remove_worktree`, `WorktreeError`, `WorktreeInfo`.
- Consumes from the codebase: `app.harness.deps.workspace_root` (a `Path`), `app.post_system(msg: str)` (async). The existing `_FakeApp` test double in `tests/test_commands.py` exposes both (`harness.deps.workspace_root` and a `posted: list[str]`).
- Produces: an async handler `_cmd_worktree(app, arg)` and a `Command("worktree", ...)` registry entry.

- [ ] **Step 1: Write the failing tests**

Add to `tests/test_commands.py` (the file already defines `_FakeApp`, `COMMANDS_BY_NAME`, `dispatch`, and imports `pytest`, `subprocess` is NOT yet imported — add `import subprocess` at the top of the file if absent):

```python
def test_worktree_registered():
    assert "worktree" in COMMANDS_BY_NAME


def test_worktree_non_git_dir_posts_error(tmp_path):
    import asyncio
    app = _FakeApp(workspace_root=tmp_path)
    asyncio.run(dispatch(app, "/worktree list"))
    assert any("Not a git repository" in m for m in app.posted)


def _git_repo(tmp_path):
    subprocess.run(["git", "init", "-q", "-b", "main"], cwd=tmp_path, check=True)
    subprocess.run(["git", "config", "user.email", "t@example.com"], cwd=tmp_path, check=True)
    subprocess.run(["git", "config", "user.name", "Test"], cwd=tmp_path, check=True)
    (tmp_path / "README.md").write_text("hi\n")
    subprocess.run(["git", "add", "."], cwd=tmp_path, check=True)
    subprocess.run(["git", "commit", "-qm", "init"], cwd=tmp_path, check=True)
    return tmp_path


def test_worktree_create_posts_launch_hint(tmp_path):
    import asyncio
    repo = _git_repo(tmp_path)
    app = _FakeApp(workspace_root=repo)
    asyncio.run(dispatch(app, "/worktree create feat/x"))
    joined = "\n".join(app.posted)
    assert "marim --worktree feat/x" in joined
    assert (repo / ".worktrees" / "feat/x").exists()


def test_worktree_create_requires_branch(tmp_path):
    import asyncio
    repo = _git_repo(tmp_path)
    app = _FakeApp(workspace_root=repo)
    asyncio.run(dispatch(app, "/worktree create"))
    assert any("Usage:" in m for m in app.posted)


def test_worktree_list_shows_branches(tmp_path):
    import asyncio
    repo = _git_repo(tmp_path)
    app = _FakeApp(workspace_root=repo)
    asyncio.run(dispatch(app, "/worktree create feat/x"))
    app.posted.clear()
    asyncio.run(dispatch(app, "/worktree list"))
    joined = "\n".join(app.posted)
    assert "main" in joined
    assert "feat/x" in joined
```

> If the existing tests use `pytest.mark.asyncio` (async test functions) rather than `asyncio.run`, follow that file's existing convention instead — check the top of `tests/test_commands.py` for an `asyncio_mode`/`pytest-asyncio` setup and mirror it. The assertions above stay the same either way.

- [ ] **Step 2: Run the tests to verify they fail**

Run: `uv run pytest tests/test_commands.py -q -k worktree`
Expected: FAIL — `worktree` not in `COMMANDS_BY_NAME`.

- [ ] **Step 3: Write the handler**

In `src/marim_harness/interfaces/tui/commands.py`, add this handler near the other `_cmd_*` functions (e.g. just before `_cmd_settings`):

```python
async def _cmd_worktree(app: HarnessApp, arg: str) -> None:
    from ...workspace.worktree import (
        WorktreeError,
        create_or_reuse_worktree,
        list_worktrees,
        remove_worktree,
        repo_root,
    )

    ws = app.harness.deps.workspace_root
    root = repo_root(ws)
    if root is None:
        await app.post_system("Not a git repository.")
        return

    sub, _, rest = arg.partition(" ")
    rest = rest.strip()
    if sub in ("", "list"):
        try:
            rows = list_worktrees(root, current=ws)
        except WorktreeError as exc:
            await app.post_system(f"Could not list worktrees: {exc}")
            return
        lines = ["| | branch | path |", "|---|---|---|"]
        for r in rows:
            marker = "•" if r.is_current else ""
            branch = r.branch or "(detached)"
            lines.append(f"| {marker} | `{branch}` | `{r.path}` |")
        await app.post_system("\n".join(lines))
    elif sub == "create":
        if not rest:
            await app.post_system("Usage: /worktree create <branch>")
            return
        try:
            path = create_or_reuse_worktree(root, rest)
        except WorktreeError as exc:
            await app.post_system(f"Could not create worktree: {exc}")
            return
        await app.post_system(
            f"Created worktree at `{path}`.\n"
            f"Launch into it with `marim --worktree {rest}` in a new terminal."
        )
    elif sub == "remove":
        if not rest:
            await app.post_system("Usage: /worktree remove <branch>")
            return
        try:
            remove_worktree(root, rest)
        except WorktreeError as exc:
            await app.post_system(f"Could not remove worktree: {exc}")
            return
        await app.post_system(f"Removed worktree for `{rest}`.")
    else:
        await app.post_system(
            "Usage: /worktree [list | create <branch> | remove <branch>]"
        )
```

- [ ] **Step 4: Register the command**

In the same file, add to the `COMMANDS` list (after the `settings` entry, before `exit`):

```python
    Command("worktree", "manage git worktrees: /worktree [list|create <b>|remove <b>]", _cmd_worktree),
```

- [ ] **Step 5: Run the tests to verify they pass**

Run: `uv run pytest tests/test_commands.py -q -k worktree`
Expected: PASS (5 tests).

- [ ] **Step 6: Run gates**

Run: `uv run ruff check src tests && uv run pyright src && uv run pytest -q`
Expected: all green.

- [ ] **Step 7: Commit**

```bash
git add src/marim_harness/interfaces/tui/commands.py tests/test_commands.py
git commit -m "feat(worktree): add /worktree list|create|remove command

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>
Claude-Session: https://claude.ai/code/session_01J1DGg5LFX9aBnYM56y1j5x"
```

---

### Task 4: Traversal hygiene (`.worktrees` exclusion + gitignore)

**Files:**
- Modify: `src/marim_harness/tools/fs.py` (`_TREE_SKIP_DIRS` at line 18; `glob_files` ~line 158; `grep` ~line 181)
- Modify: `.gitignore`
- Test: `tests/test_fs.py` (add to the existing file)

**Interfaces:**
- Consumes: nothing from other tasks. Uses the literal string `".worktrees"` (matching the existing `_TREE_SKIP_DIRS` literal style; no import from `worktree.py`).
- Produces: `tree`, `glob_files`, and `grep` all skip any path with a `.worktrees` component.

> **Why all three:** `_TREE_SKIP_DIRS` is consumed only by `_walk_tree` (`fs.py:152`). `grep` walks via `base.rglob("*")` (`fs.py:185`) and `glob_files` via `root.glob(pattern)` (`fs.py:161`) — neither consults the skip set, so without an explicit exclusion they descend into sibling worktree checkouts and return duplicate/cross-branch matches.

- [ ] **Step 1: Write the failing tests**

Add to `tests/test_fs.py`:

```python
def test_tree_lists_but_does_not_descend_worktrees(tmp_path: Path):
    wt = tmp_path / ".worktrees" / "feat-x"
    wt.mkdir(parents=True)
    (wt / "secret.txt").write_text("x")
    out = fs.tree(tmp_path, ".", depth=3)
    assert ".worktrees/" in out
    assert "secret.txt" not in out


def test_grep_skips_worktrees(tmp_path: Path):
    (tmp_path / "main.txt").write_text("needle here\n")
    wt = tmp_path / ".worktrees" / "feat-x"
    wt.mkdir(parents=True)
    (wt / "copy.txt").write_text("needle here\n")
    out = fs.grep(tmp_path, "needle")
    assert "main.txt" in out
    assert ".worktrees" not in out


def test_glob_skips_worktrees(tmp_path: Path):
    (tmp_path / "a.py").write_text("x")
    wt = tmp_path / ".worktrees" / "feat-x"
    wt.mkdir(parents=True)
    (wt / "b.py").write_text("x")
    out = fs.glob_files(tmp_path, "**/*.py")
    assert "a.py" in out
    assert ".worktrees" not in out
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `uv run pytest tests/test_fs.py -q -k worktree`
Expected: FAIL — grep/glob include `.worktrees` paths; tree may already pass for `secret.txt` only if depth excludes it, so expect at least the grep and glob tests to fail.

- [ ] **Step 3: Add `.worktrees` to the tree skip set**

In `src/marim_harness/tools/fs.py`, change `_TREE_SKIP_DIRS` (line 18) to include `".worktrees"`:

```python
_TREE_SKIP_DIRS = {
    ".git", "node_modules", "__pycache__", ".venv", ".mypy_cache",
    ".pytest_cache", ".ruff_cache", "dist", "build", ".egg-info",
    ".worktrees",
}
```

- [ ] **Step 4: Exclude `.worktrees` from `glob_files`**

In `glob_files`, inside the `for p in candidates:` loop, after the `if not p.is_file(): continue` check, add:

```python
        if ".worktrees" in p.relative_to(root).parts:
            continue  # skip sibling worktree checkouts
```

- [ ] **Step 5: Exclude `.worktrees` from `grep`**

In `grep`, at the top of the `for f in files:` loop body (before the existing resolve-in-workspace guard), add:

```python
        if ".worktrees" in f.relative_to(root).parts:
            continue  # skip sibling worktree checkouts
```

- [ ] **Step 6: Add `.worktrees/` to `.gitignore`**

Append to `.gitignore`:

```
# Git worktrees created by --worktree / the /worktree command.
.worktrees/
```

- [ ] **Step 7: Run the tests to verify they pass**

Run: `uv run pytest tests/test_fs.py -q -k worktree`
Expected: PASS (3 tests).

- [ ] **Step 8: Run gates**

Run: `uv run ruff check src tests && uv run pyright src && uv run pytest -q`
Expected: all green.

- [ ] **Step 9: Commit**

```bash
git add src/marim_harness/tools/fs.py tests/test_fs.py .gitignore
git commit -m "feat(worktree): skip .worktrees in tree/grep/glob and gitignore it

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>
Claude-Session: https://claude.ai/code/session_01J1DGg5LFX9aBnYM56y1j5x"
```

---

## Notes for the executor

- Tasks 2, 3, and 4 each depend only on Task 1, not on each other — but execute them in order; each ends with a green full suite.
- `git worktree add -b <branch> <path>` creates the branch from current HEAD; `git worktree add <path> <branch>` checks out an existing branch. The `-b` form is for new branches only — `create_or_reuse_worktree` picks the form via `_branch_exists`.
- The temp-repo fixtures use `git init -b main` (git ≥ 2.28); CI has a modern git. If a worktree test ever needs the default-branch name, read it rather than hardcoding.
- `.marim/` runtime dirs are never staged with `git add -A` — the commits above stage named files only.
