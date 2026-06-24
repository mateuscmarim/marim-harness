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


def test_restore_returns_true_on_success(tmp_path: Path):
    repo = _init_repo(tmp_path)
    snap = GitSnapshotter(repo)
    commit = snap.capture("refs/marim/checkpoints/s/0", "cp 0")
    assert commit
    (repo / "a.txt").write_text("MODIFIED\n")
    assert snap.restore(commit) is True


def test_restore_returns_false_outside_git(tmp_path: Path):
    plain = tmp_path / "plain"
    plain.mkdir()
    assert GitSnapshotter(plain).restore("deadbeef") is False  # must not raise


def test_restore_returns_false_on_bad_commit(tmp_path: Path):
    repo = _init_repo(tmp_path)
    # A nonexistent commit makes read-tree fail; restore must report failure, not
    # silently return as if it succeeded.
    assert GitSnapshotter(repo).restore("0" * 40) is False


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


def test_restore_handles_filename_with_newline(tmp_path: Path):
    """Regression: parsing ls-tree/ls-files with .splitlines() shatters a filename
    containing a newline into bogus entries — and since this drives a DELETE path,
    it could unlink the wrong file. With NUL-delimited (-z) parsing, a snapshot of
    a newline-named file restores correctly and a post-checkpoint newline-named
    file is removed."""
    repo = _init_repo(tmp_path)
    snap = GitSnapshotter(repo)
    weird = "a\nb.txt"  # filename with an embedded newline
    (repo / weird).write_text("captured\n")
    commit = snap.capture("refs/marim/checkpoints/s/0", "cp 0")
    assert commit
    # Mutate after the checkpoint, then restore: the snapshot's content must come
    # back and the newline-named file must not be misparsed/deleted.
    (repo / weird).write_text("CHANGED\n")
    assert snap.restore(commit) is True
    assert (repo / weird).read_text() == "captured\n"


def test_present_files_parses_newline_names_without_splitting(tmp_path: Path):
    """_present_files must treat a newline-containing name as ONE path, not two."""
    repo = _init_repo(tmp_path)
    weird = "x\ny.txt"
    (repo / weird).write_text("hi\n")
    present = GitSnapshotter(repo)._present_files()
    assert weird in present
    # The bogus fragments a .splitlines() parse would have produced are absent.
    assert "x" not in present
    assert "y.txt" not in present


def test_restore_returns_false_when_a_stale_file_cannot_be_removed(
    tmp_path: Path, monkeypatch
):
    """Regression: the delete loop suppressed OSError and still returned True, so a
    partial restore (a file that couldn't be removed) was reported as success. Now
    an unlink failure surfaces as restore()==False."""
    import pathlib

    repo = _init_repo(tmp_path)
    snap = GitSnapshotter(repo)
    commit = snap.capture("refs/marim/checkpoints/s/0", "cp 0")
    # Create a file AFTER the checkpoint; restore would try to delete it.
    (repo / "after.txt").write_text("created post-checkpoint\n")

    real_unlink = pathlib.Path.unlink

    def flaky_unlink(self, *a, **k):
        if self.name == "after.txt":
            raise PermissionError("cannot remove")
        return real_unlink(self, *a, **k)

    monkeypatch.setattr(pathlib.Path, "unlink", flaky_unlink)
    # The unlink of after.txt fails -> partial restore -> must report False.
    assert snap.restore(commit) is False


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
