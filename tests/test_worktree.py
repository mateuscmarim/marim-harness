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


def test_remove_resolves_reused_external_worktree(repo: Path, tmp_path: Path):
    """create_or_reuse returns ANY worktree already on the branch — including one
    outside .worktrees/. remove must target that real path, not assume the
    canonical .worktrees/<branch> location (which would error on a missing dir)."""
    external = tmp_path.with_name(tmp_path.name + "_ext")
    subprocess.run(
        ["git", "worktree", "add", str(external), "-b", "feat/ext"],
        cwd=repo, check=True, capture_output=True,
    )
    # git won't allow a second checkout of the branch, so create reuses it.
    path = wt.create_or_reuse_worktree(repo, "feat/ext")
    assert path.resolve() == external.resolve()
    # remove must find and remove that real path.
    wt.remove_worktree(repo, "feat/ext")
    assert not external.exists()


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


def test_repo_root_from_inside_linked_worktree(repo: Path):
    """repo_root must return the MAIN worktree toplevel even when called from
    inside a linked worktree — not the linked worktree's own path."""
    linked = wt.create_or_reuse_worktree(repo, "feat/x")
    assert wt.repo_root(linked) == repo.resolve()
