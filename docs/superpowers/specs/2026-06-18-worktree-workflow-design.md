# Git Worktree Support — Sub-project A: Human Worktree Workflow — Design

**Date:** 2026-06-18
**Status:** Approved (pending spec review)

## Context

"Worktree support" for marim was scoped into two independent sub-projects:

- **A (this spec) — Human worktree workflow:** marim can create and operate
  inside git worktrees, driven by the user.
- **B (future) — Subagent worktree isolation:** `spawn_agent` runs subagents in
  their own worktrees. Builds on A's git plumbing. Separate spec.

This spec covers **A only**.

The key constraint from the codebase: `Deps.workspace_root`
(`src/marim_harness/deps.py:38`) is set once at bootstrap
(`src/marim_harness/bootstrap.py:43`) and is **immutable** for the life of the
harness. Sessions are scoped per workspace path via a sha256 hash
(`src/marim_harness/session/store.py:22-25`). Subagents share the parent's
`workspace_root` (`src/marim_harness/subagents.py`). There is currently **no**
git or worktree code anywhere in the repo.

Because `workspace_root` is immutable, "working in" a worktree happens at
**launch time**, not by switching a live session. This was a deliberate design
choice over in-process rebuild (too risky: would tear down/rebuild live LSP,
MCP, bash, and session state) and sibling-process spawning (marim is a
full-screen Textual TUI; spawning a second TUI fights over the terminal). The
launch-time approach also builds exactly the git plumbing that sub-project B
reuses.

## Goal

A `--worktree <branch>` launch flag that runs the session inside
`<repo>/.worktrees/<branch>`, plus a `/worktree` command to list, create, and
remove worktrees from within a session. `workspace_root` stays immutable;
entering a worktree is a launch-time action.

## Architecture

Four pieces:

1. `workspace/worktree.py` — a new UI-agnostic git-worktree module (the only
   place that shells out to `git` for worktree ops). Pure functions + a small
   dataclass and error type. No Textual, no agent, no `Deps` imports.
2. CLI wiring in `interfaces/cli/default_cmd.py` — a `--worktree` flag that
   resolves the effective workspace to a worktree path before `build_harness`.
3. A `/worktree` command in `interfaces/tui/commands.py`.
4. Traversal hygiene — exclude `.worktrees/` from file-tool descent and from
   git tracking.

Everything downstream of `workspace_root` (Deps, session hashing, tools, LSP,
MCP) is unchanged: pointing it at a worktree path scopes all of them
automatically.

## Component 1 — `src/marim_harness/workspace/worktree.py`

```python
"""Git worktree operations: create/list/remove worktrees under <repo>/.worktrees.

UI-agnostic. The only module that shells out to ``git`` for worktree management.
Every function takes an already-resolved repo root (see ``repo_root``). Git
failures surface as ``WorktreeError`` with git's stderr; callers decide how to
present them. Nothing here imports Deps, Textual, or the agent.
"""

WORKTREES_DIRNAME = ".worktrees"

@dataclass(frozen=True)
class WorktreeInfo:
    path: Path        # absolute worktree path
    branch: str       # branch name (without refs/heads/), or "" if detached
    head: str         # commit sha
    is_current: bool  # True if path == the `current` arg passed to list_worktrees

class WorktreeError(Exception):
    """A git worktree operation failed; message carries git's stderr."""

def repo_root(path: Path) -> Path | None:
    """`git -C <path> rev-parse --show-toplevel`, or None if not a git repo
    (or git is not installed). The **main** worktree's root."""

def list_worktrees(repo_root: Path) -> list[WorktreeInfo]:
    """Parse `git worktree list --porcelain` into WorktreeInfo rows.
    `is_current` is set for the row whose path == the given `current` arg."""
    # signature in code: list_worktrees(repo_root, current: Path | None = None)

def create_or_reuse_worktree(repo_root: Path, branch: str) -> Path:
    """Return the worktree path for `branch` under <repo_root>/.worktrees/<branch>.
    - If a worktree for `branch` already exists, return its path (idempotent).
    - Else if branch `branch` exists, add a new worktree checking it out there.
    - Else create `branch` from current HEAD and add the worktree.
    Raises WorktreeError on any git failure (e.g. branch checked out elsewhere)
    or if `branch` fails validation."""

def remove_worktree(repo_root: Path, branch: str) -> None:
    """`git worktree remove <repo_root>/.worktrees/<branch>`. Refuses if the
    worktree is dirty or is the current one (git's own rules). Never deletes the
    branch. Raises WorktreeError on failure."""

def _validate_branch(branch: str) -> None:
    """Reject empty, leading '-', or any '..' path segment (escape guard).
    Slashes are allowed (e.g. 'feat/x' nests under .worktrees/feat/x)."""
```

Behavior details:

- **Base for a new branch** is the current HEAD of the repo (normally
  main/master — whatever is checked out in the main working tree). This matches
  ordinary `git branch <new>` flow; no separate base-detection logic.
- All git calls use `subprocess.run([...], cwd=repo_root, capture_output=True,
  text=True)` with a list argv (never `shell=True`). On non-zero return,
  raise `WorktreeError(result.stderr.strip() or result.stdout.strip())`.
- `repo_root` swallows `FileNotFoundError` (git not installed) and non-zero
  exit, returning `None`.
- The worktree directory is `repo_root / WORKTREES_DIRNAME / branch`. Parent
  dirs for nested branch names are created by `git worktree add` itself.
- `create_or_reuse_worktree` detects an existing worktree by scanning
  `list_worktrees(repo_root)` for a row whose `branch == branch`; if found,
  returns that row's path without invoking `git worktree add` again
  (idempotency lets re-launching `--worktree X` Just Work).
- Branch existence is checked with
  `git show-ref --verify --quiet refs/heads/<branch>` (exit 0 = exists).

## Component 2 — CLI flag (`interfaces/cli/default_cmd.py`)

In `_build_parser()` add:

```python
parser.add_argument(
    "--worktree",
    metavar="BRANCH",
    help="Run inside a git worktree for BRANCH under <repo>/.worktrees/, "
         "creating it (from current HEAD) or reusing it.",
)
```

In the run path, after computing `workspace` (the resolved positional/cwd,
`default_cmd.py:54`) and **before** `build_harness`:

```python
if args.worktree:
    from ...workspace.worktree import repo_root, create_or_reuse_worktree, WorktreeError
    root = repo_root(workspace)
    if root is None:
        parser.error(f"--worktree: {workspace} is not a git repository")
    try:
        workspace = create_or_reuse_worktree(root, args.worktree)
    except WorktreeError as exc:
        parser.error(f"--worktree: {exc}")
```

`parser.error(...)` prints to stderr and exits non-zero — the right behavior for
a launch-time failure. `workspace` is then the worktree path for the rest of the
function (headless or TUI), so Deps/session/tools all scope to it with no
further change.

## Component 3 — `/worktree` command (`interfaces/tui/commands.py`)

```python
async def _cmd_worktree(app: HarnessApp, arg: str) -> None:
    from ...workspace.worktree import (
        repo_root, list_worktrees, create_or_reuse_worktree,
        remove_worktree, WorktreeError,
    )
    ws = app.harness.deps.workspace_root
    root = repo_root(ws)
    if root is None:
        await app.post_system("Not a git repository.")
        return
    sub, _, rest = arg.strip().partition(" ")
    rest = rest.strip()
    if sub in ("", "list"):
        rows = list_worktrees(root, current=ws)
        # render a markdown table: marker, branch, path
        ...
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
        await app.post_system("Usage: /worktree [list | create <branch> | remove <branch>]")
```

Registered in `COMMANDS`:
`Command("worktree", "manage git worktrees: /worktree [list|create <b>|remove <b>]", _cmd_worktree)`.

`/worktree create` does **not** switch the running session — it only creates the
worktree and tells the user how to launch into it. This is the deliberate
launch-time model.

## Component 4 — Traversal hygiene

- `src/marim_harness/tools/fs.py:18` — add `".worktrees"` to `_TREE_SKIP_DIRS`
  so `tree` lists but never descends into worktrees when marim runs in the main
  repo. Confirm during implementation whether `grep`/`glob` share this skip set;
  if they walk independently, apply the same `.worktrees` exclusion there. (The
  plan will pin the exact call sites.)
- Repo `.gitignore` — add a `.worktrees/` entry so worktree directories are
  never accidentally tracked.

## Data Flow

```
marim --worktree feat/x         [cwd = main repo]
  └─ default_cmd: repo_root(cwd) → create_or_reuse_worktree(repo, "feat/x")
       → <repo>/.worktrees/feat/x
       └─ build_harness(<repo>/.worktrees/feat/x)
            └─ Deps.workspace_root = worktree; session hash, tools, LSP all
               scope to the worktree (no further changes)

/worktree create feat/y          [inside a running session]
  └─ repo_root(workspace_root) → create_or_reuse_worktree → post launch command
                                                            (no session switch)
/worktree list                   → git worktree list --porcelain → table
/worktree remove feat/y          → git worktree remove (refuses current/dirty)
```

## Error Handling

| Situation | Behavior |
|-----------|----------|
| `--worktree` in a non-git dir | `parser.error` → stderr message, exit non-zero |
| git not installed | `repo_root` returns None → treated as "not a git repository" |
| Branch already checked out elsewhere | git fails → `WorktreeError` → flag exits / command posts error |
| `/worktree remove` of current or dirty worktree | git refuses → `WorktreeError` surfaced; no crash |
| Branch name with `..` or leading `-` | `_validate_branch` raises `WorktreeError` before any git call |
| Any `/worktree` git failure | caught, posted as a system message; TUI never crashes |

## Testing

`tests/test_worktree.py` — unit tests against a **real temporary git repo**
(git is available in CI; create with `git init`, configure a user, one commit):

- `repo_root` returns the toplevel inside a repo, `None` outside one.
- `create_or_reuse_worktree` with a new branch: creates the branch and a
  worktree dir at `.worktrees/<branch>`; the dir is a valid checkout.
- `create_or_reuse_worktree` is idempotent: a second call for the same branch
  returns the same path and does not error.
- `create_or_reuse_worktree` with a pre-existing branch attaches a worktree
  checking out that branch.
- `list_worktrees` returns the main worktree plus created ones; `is_current` is
  set correctly when `current=` is passed.
- `remove_worktree` deletes the worktree dir but the branch still exists
  (`git show-ref` still resolves it).
- `remove_worktree` raises `WorktreeError` for the main/current worktree.
- `_validate_branch` rejects `"../escape"`, `""`, and `"-x"`; accepts
  `"feat/x"`.

`tests/test_commands.py` (or the command-dispatch test file) — `/worktree`
resolves in `COMMANDS_BY_NAME`; `create` posts a message containing
`marim --worktree`; in a non-git temp dir the handler posts "Not a git
repository." (use a fake/minimal app or the existing command-test harness).

Tree traversal — extend the existing `tree` test (or add one) asserting a
`.worktrees/` directory under the workspace is listed but not descended into.

Gates: `uv run ruff check src tests`, `uv run pyright src`, `uv run pytest` all
green.

## Out of Scope (deferred)

- In-process or sibling-process session switching (flag-only launch model now).
- Subagent worktree isolation — **sub-project B**, separate spec, reuses
  `workspace/worktree.py`.
- Auto-deleting a branch when its worktree is removed — branch lifecycle belongs
  to the user / the finishing-a-development-branch flow.
- Merging a worktree's branch back to main — git's / the user's job.
- A config override for the `.worktrees` location — hardcode the convention;
  revisit only if a real need appears (YAGNI).
