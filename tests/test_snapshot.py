# tests/test_snapshot.py
import subprocess
from pathlib import Path

from marim_harness.workspace.snapshot import GitSnapshotter


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", *args], cwd=repo, capture_output=True, text=True, check=True
    ).stdout.strip()


def _init_repo(tmp_path: Path) -> Path:
    repo = tmp_path / "ws"
    repo.mkdir()
    _git(repo, "init", "-q")
    _git(repo, "config", "user.email", "t@t")
    _git(repo, "config", "user.name", "t")
    (repo / "a.txt").write_text("one\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", "init")
    return repo


def test_capture_returns_commit_and_sets_ref(tmp_path: Path):
    repo = _init_repo(tmp_path)
    (repo / "a.txt").write_text("changed\n")
    (repo / "new.txt").write_text("fresh\n")  # untracked, non-ignored
    snap = GitSnapshotter(repo)
    commit = snap.capture("refs/marim/checkpoints/s/0", "cp 0")
    assert commit
    # The ref resolves to the commit, keeping it alive.
    assert _git(repo, "rev-parse", "refs/marim/checkpoints/s/0") == commit
    # The snapshot tree contains both the modified and the untracked file.
    listed = _git(repo, "ls-tree", "-r", "--name-only", commit)
    assert "a.txt" in listed and "new.txt" in listed


def test_capture_does_not_touch_user_branch_or_index(tmp_path: Path):
    repo = _init_repo(tmp_path)
    head_before = _git(repo, "rev-parse", "HEAD")
    (repo / "a.txt").write_text("changed\n")
    GitSnapshotter(repo).capture("refs/marim/checkpoints/s/0", "cp 0")
    assert _git(repo, "rev-parse", "HEAD") == head_before          # HEAD unmoved
    assert _git(repo, "status", "--porcelain")                     # change still unstaged/dirty
    assert "changed" in (repo / "a.txt").read_text()               # working tree untouched
    assert _git(repo, "diff", "--cached", "--name-only") == ""   # real index untouched


def test_capture_returns_none_outside_git(tmp_path: Path):
    plain = tmp_path / "plain"
    plain.mkdir()
    assert GitSnapshotter(plain).capture("refs/marim/checkpoints/s/0", "cp") is None


def test_capture_rejects_ref_outside_marim_namespace(tmp_path: Path):
    repo = _init_repo(tmp_path)
    import pytest
    with pytest.raises(ValueError):
        GitSnapshotter(repo).capture("refs/heads/main", "cp")


def test_restore_reverts_modification(tmp_path: Path):
    repo = _init_repo(tmp_path)
    snap = GitSnapshotter(repo)
    commit = snap.capture("refs/marim/checkpoints/s/0", "cp 0")
    (repo / "a.txt").write_text("MODIFIED\n")
    snap.restore(commit)
    assert (repo / "a.txt").read_text() == "one\n"


def test_restore_recreates_deleted_file(tmp_path: Path):
    repo = _init_repo(tmp_path)
    snap = GitSnapshotter(repo)
    commit = snap.capture("refs/marim/checkpoints/s/0", "cp 0")
    (repo / "a.txt").unlink()
    snap.restore(commit)
    assert (repo / "a.txt").read_text() == "one\n"


def test_restore_removes_file_created_after_checkpoint(tmp_path: Path):
    repo = _init_repo(tmp_path)
    snap = GitSnapshotter(repo)
    commit = snap.capture("refs/marim/checkpoints/s/0", "cp 0")
    (repo / "after.txt").write_text("should be gone\n")
    snap.restore(commit)
    assert not (repo / "after.txt").exists()


def test_restore_writes_pre_restore_safety_snapshot(tmp_path: Path):
    repo = _init_repo(tmp_path)
    snap = GitSnapshotter(repo)
    commit = snap.capture("refs/marim/checkpoints/s/0", "cp 0")
    (repo / "a.txt").write_text("DANGER\n")
    snap.restore(commit)
    # The pre-restore state is recoverable from the safety ref.
    pre = _git(repo, "rev-parse", "refs/marim/checkpoints/_pre_restore")
    blob = _git(repo, "show", f"{pre}:a.txt")
    assert blob == "DANGER"


def test_restore_is_noop_outside_git(tmp_path: Path):
    plain = tmp_path / "plain"
    plain.mkdir()
    GitSnapshotter(plain).restore("deadbeef")  # must not raise


def test_restore_does_not_touch_index_or_head(tmp_path: Path):
    repo = _init_repo(tmp_path)
    snap = GitSnapshotter(repo)
    commit = snap.capture("refs/marim/checkpoints/s/0", "cp 0")
    (repo / "a.txt").write_text("changed\n")
    head_before = _git(repo, "rev-parse", "HEAD")
    snap.restore(commit)
    assert _git(repo, "rev-parse", "HEAD") == head_before        # HEAD unmoved
    assert _git(repo, "diff", "--cached", "--name-only") == ""    # real index untouched


def test_delete_removes_ref(tmp_path: Path):
    repo = _init_repo(tmp_path)
    snap = GitSnapshotter(repo)
    snap.capture("refs/marim/checkpoints/s/0", "cp 0")
    snap.delete("refs/marim/checkpoints/s/0")
    result = subprocess.run(
        ["git", "rev-parse", "refs/marim/checkpoints/s/0"],
        cwd=repo, capture_output=True, text=True,
    )
    assert result.returncode != 0  # ref is gone


def test_delete_rejects_ref_outside_marim_namespace(tmp_path: Path):
    repo = _init_repo(tmp_path)
    import pytest
    with pytest.raises(ValueError):
        GitSnapshotter(repo).delete("refs/heads/main")


def test_capture_restore_act_on_linked_worktree(tmp_path: Path):
    """Regression: GitSnapshotter(wt) must capture/restore the LINKED worktree,
    not the main worktree toplevel returned by repo_root()."""
    main = _init_repo(tmp_path)
    wt = tmp_path / "wt"
    _git(main, "worktree", "add", "-q", str(wt), "-b", "feat")
    # Record main's state before any worktree edits.
    main_before = (main / "a.txt").read_text()
    # Edit a.txt only in the linked worktree.
    (wt / "a.txt").write_text("wt-before\n")
    snap = GitSnapshotter(wt)
    commit = snap.capture("refs/marim/checkpoints/s/0", "cp")
    assert commit, "capture should return a commit sha"
    # The snapshot must contain the WORKTREE's content, not the main checkout's.
    assert _git(wt, "show", f"{commit}:a.txt") == "wt-before"
    # Now advance the linked worktree past the checkpoint.
    (wt / "a.txt").write_text("wt-after\n")
    snap.restore(commit)
    # The linked worktree must be reverted to the captured state.
    assert (wt / "a.txt").read_text() == "wt-before\n"
    # The main worktree must be completely untouched.
    assert (main / "a.txt").read_text() == main_before
