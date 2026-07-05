"""Unit tests for SpawnWorktree — the value object that owns a spawn's isolated
git worktree lifecycle: open/reopen, commit-and-close, and the fresh-vs-resumed
teardown policy that the runner's foreground/background/CLI failure arms share.

These pin the object in isolation; the end-to-end wiring stays covered by
tests/test_subagent_isolation.py."""

import subprocess
from pathlib import Path

import pytest

from marim_harness.subagents.isolation import SpawnWorktree
from marim_harness.tools.impl import fs


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    """A real git repo with one commit, on branch main."""
    subprocess.run(["git", "init", "-q", "-b", "main"], cwd=tmp_path, check=True)
    subprocess.run(["git", "config", "user.email", "t@example.com"], cwd=tmp_path, check=True)
    subprocess.run(["git", "config", "user.name", "Test"], cwd=tmp_path, check=True)
    (tmp_path / "README.md").write_text("hi\n")
    subprocess.run(["git", "add", "."], cwd=tmp_path, check=True)
    subprocess.run(["git", "commit", "-qm", "init"], cwd=tmp_path, check=True)
    return tmp_path


def _branch_exists(repo: Path, branch: str) -> bool:
    out = subprocess.run(["git", "branch", "--list", branch], cwd=repo,
                         capture_output=True, text=True).stdout
    return out.strip() != ""


def test_open_creates_worktree_on_a_new_branch(repo: Path):
    wt, err = SpawnWorktree.open(repo, "subagent/tc1")
    assert err is None
    assert wt is not None
    assert wt.repo == repo
    assert wt.branch == "subagent/tc1"
    assert ".worktrees" in str(wt.path)
    assert wt.path.exists()
    assert _branch_exists(repo, "subagent/tc1")


def test_open_outside_a_git_repo_returns_an_error(tmp_path: Path):
    wt, err = SpawnWorktree.open(tmp_path, "subagent/tc1")
    assert wt is None
    assert err is not None and "git" in err.lower()


def test_close_commits_changes_and_reports_the_branch(repo: Path):
    wt, _ = SpawnWorktree.open(repo, "subagent/tc1")
    fs.write_file(wt.path, "new.txt", "from sub-agent\n")
    note = wt.close()
    assert "subagent/tc1" in note        # branch named
    assert "new.txt" in note             # diffstat included
    assert not wt.path.exists()          # worktree torn down
    show = subprocess.run(["git", "show", "--stat", "subagent/tc1"], cwd=repo,
                          capture_output=True, text=True)
    assert show.returncode == 0 and "new.txt" in show.stdout


def test_close_with_no_changes_drops_the_branch(repo: Path):
    wt, _ = SpawnWorktree.open(repo, "subagent/tc1")
    note = wt.close()
    assert "no file changes" in note
    assert not _branch_exists(repo, "subagent/tc1")
    assert not wt.path.exists()


def test_teardown_after_fresh_failure_drops_worktree_and_branch(repo: Path):
    wt, _ = SpawnWorktree.open(repo, "subagent/tc1")
    fs.write_file(wt.path, "partial.txt", "half\n")   # dirty, unwanted
    wt.teardown_after_failure(resumed=False)
    assert not _branch_exists(repo, "subagent/tc1")
    assert not wt.path.exists()


def test_teardown_after_resumed_failure_keeps_the_branch(repo: Path):
    subprocess.run(["git", "branch", "subagent/sg6"], cwd=repo, check=True)
    wt, _ = SpawnWorktree.reopen(repo, "subagent/sg6")
    fs.write_file(wt.path, "partial.txt", "half\n")
    wt.teardown_after_failure(resumed=True)
    assert _branch_exists(repo, "subagent/sg6")       # deliverable survives
    assert not wt.path.exists()                        # checkout gone


def test_reopen_an_existing_branch(repo: Path):
    subprocess.run(["git", "branch", "subagent/sg6"], cwd=repo, check=True)
    wt, err = SpawnWorktree.reopen(repo, "subagent/sg6")
    assert err is None and wt is not None
    assert wt.branch == "subagent/sg6"
    assert wt.path.exists()


def test_reopen_a_missing_branch_returns_an_error(repo: Path):
    wt, err = SpawnWorktree.reopen(repo, "subagent/gone")
    assert wt is None
    assert err is not None and "no longer exists" in err
