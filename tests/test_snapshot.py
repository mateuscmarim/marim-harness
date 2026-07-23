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


def test_restore_succeeds_with_nested_worktree(tmp_path: Path):
    """Regression: a nested git repo in the workspace — including marim's own
    ``.worktrees/<branch>`` spawn worktrees, which nothing gitignores — is staged
    as a gitlink (``.worktrees/feat``) but reported by ``ls-files --others`` with a
    trailing slash (``.worktrees/feat/``). The mismatch made the set difference mark
    it for deletion; unlink() of a directory raised, and restore() returned False on
    EVERY rewind forever. Now the nested worktree is left alone and restore
    succeeds."""
    repo = _init_repo(tmp_path)
    # A prior sub-agent spawn left a nested worktree; it exists at capture time.
    _git(repo, "worktree", "add", "-q", str(repo / ".worktrees" / "feat"), "-b", "feat")
    snap = GitSnapshotter(repo)
    commit = snap.capture("refs/marim/checkpoints/s/0", "cp 0")
    assert commit
    (repo / "a.txt").write_text("MODIFIED\n")
    assert snap.restore(commit) is True          # not False-forever
    assert (repo / "a.txt").read_text() == "one\n"  # the file restore still worked
    assert (repo / ".worktrees" / "feat").is_dir()  # nested worktree untouched


def test_restore_succeeds_with_nested_repo_created_after_checkpoint(tmp_path: Path):
    """The nested repo can also post-date the checkpoint (absent from the tree).
    It must still never be treated as a deletable file — restore leaves it and
    reports success."""
    repo = _init_repo(tmp_path)
    snap = GitSnapshotter(repo)
    commit = snap.capture("refs/marim/checkpoints/s/0", "cp 0")
    assert commit
    _git(repo, "worktree", "add", "-q", str(repo / ".worktrees" / "feat"), "-b", "feat")
    assert snap.restore(commit) is True
    assert (repo / ".worktrees" / "feat").is_dir()


def test_capture_works_without_git_identity(tmp_path: Path, monkeypatch):
    """Regression: commit-tree ran with no committer identity, so on a machine/CI
    without user.name/user.email it raised → capture returned None → checkpoints
    and file-rewind were silently dead. An inline identity makes it work
    regardless of git config."""
    empty = tmp_path / "empty.cfg"
    empty.write_text("")
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(empty))
    monkeypatch.setenv("GIT_CONFIG_SYSTEM", str(empty))
    for var in (
        "GIT_AUTHOR_NAME", "GIT_AUTHOR_EMAIL",
        "GIT_COMMITTER_NAME", "GIT_COMMITTER_EMAIL",
    ):
        monkeypatch.delenv(var, raising=False)
    repo = tmp_path / "ws"
    repo.mkdir()
    # NOTE: no `git config user.*` — the whole point is an identity-less repo.
    _git(repo, "init", "-q")
    (repo / "a.txt").write_text("one\n")

    commit = GitSnapshotter(repo).capture("refs/marim/checkpoints/s/0", "cp")
    assert commit is not None  # commit-tree no longer needs configured identity


def test_restore_does_not_delete_a_file_ignored_at_capture(tmp_path: Path):
    """Regression / data-safety: a file git-ignored at CAPTURE time (never in the
    snapshot) that becomes un-ignored at RESTORE time — because the agent deleted
    .gitignore — used to be listed by ``ls-files --others`` and silently deleted on
    rewind (.env, local DBs). Restoring the captured tree (which reinstates the
    capture-time .gitignore) before computing extras keeps such files safe."""
    repo = _init_repo(tmp_path)
    (repo / ".gitignore").write_text(".env\n")
    (repo / ".env").write_text("SECRET\n")  # ignored — never captured
    _git(repo, "add", ".gitignore")
    _git(repo, "commit", "-qm", "ignore env")

    snap = GitSnapshotter(repo)
    commit = snap.capture("refs/marim/checkpoints/s/0", "cp")
    assert commit
    # After the checkpoint the agent deletes .gitignore and edits a tracked file.
    (repo / ".gitignore").unlink()
    (repo / "a.txt").write_text("MODIFIED\n")

    assert snap.restore(commit) is True
    assert (repo / ".env").read_text() == "SECRET\n"       # NOT deleted
    assert (repo / "a.txt").read_text() == "one\n"          # tracked file reverted
    assert (repo / ".gitignore").read_text() == ".env\n"    # gitignore restored


def test_tracked_but_ignored_file_is_captured_and_survives_restore(tmp_path: Path):
    """Regression / data-safety: a file that is gitignored yet TRACKED (forced in
    with ``git add -f``, e.g. a committed .env) was skipped by capture's ``add -A``
    against the fresh throwaway index (ignore rules apply because the fresh index
    doesn't know the file is tracked) — but listed by ``ls-files`` against the REAL
    index in _present_files. Absent from the snapshot yet "present", the
    set-difference marked it extra and EVERY restore permanently deleted it."""
    repo = _init_repo(tmp_path)
    (repo / ".gitignore").write_text(".env\n")
    (repo / ".env").write_text("SECRET\n")
    _git(repo, "add", ".gitignore")
    _git(repo, "add", "-f", ".env")  # ignored, but force-tracked
    _git(repo, "commit", "-qm", "track env")

    snap = GitSnapshotter(repo)
    # Dirty the tracked-ignored file so capture must take the full (add -A) path
    # and record the LIVE content, not just reuse HEAD.
    (repo / ".env").write_text("SECRET-v2\n")
    commit = snap.capture("refs/marim/checkpoints/s/0", "cp")
    assert commit
    # The tracked-ignored file must be IN the snapshot tree, at live content.
    assert _git(repo, "show", f"{commit}:.env") == "SECRET-v2"

    # After the checkpoint the agent edits both files; rewind must revert them —
    # and above all must NOT delete .env.
    (repo / ".env").write_text("CLOBBERED\n")
    (repo / "a.txt").write_text("MODIFIED\n")
    assert snap.restore(commit) is True
    assert (repo / ".env").read_text() == "SECRET-v2\n"  # restored, NOT deleted
    assert (repo / "a.txt").read_text() == "one\n"


def test_untracked_ignored_file_stays_out_of_snapshot_and_survives_restore(tmp_path: Path):
    """The complement of the tracked-ignored fix: a *plain* ignored file (never
    added) must remain OUT of the snapshot and must NOT be deleted on restore.
    Guards the fix from over-correcting into capturing all ignored files."""
    repo = _init_repo(tmp_path)
    (repo / ".gitignore").write_text(".env\n")
    (repo / ".env").write_text("SECRET\n")  # ignored and never tracked
    _git(repo, "add", ".gitignore")
    _git(repo, "commit", "-qm", "ignore env")

    snap = GitSnapshotter(repo)
    (repo / "a.txt").write_text("dirty\n")  # force the full capture path
    commit = snap.capture("refs/marim/checkpoints/s/0", "cp")
    assert commit
    assert ".env" not in _git(repo, "ls-tree", "-r", "--name-only", commit)
    (repo / "a.txt").write_text("MODIFIED\n")
    assert snap.restore(commit) is True
    assert (repo / ".env").read_text() == "SECRET\n"  # untouched


def test_restore_removes_symlink_to_directory_created_after_checkpoint(tmp_path: Path):
    """Regression: _remove_extra_files used ``is_dir()``, which FOLLOWS symlinks —
    a post-checkpoint symlink pointing at a directory was misclassified as a
    nested repo, skipped, and restore still returned True (silently incomplete).
    A symlink is a file-sized entry; it must be unlinked like any extra file."""
    repo = _init_repo(tmp_path)
    snap = GitSnapshotter(repo)
    commit = snap.capture("refs/marim/checkpoints/s/0", "cp 0")
    assert commit
    target = repo / "real_dir"
    target.mkdir()
    (repo / "link_dir").symlink_to(target, target_is_directory=True)
    assert snap.restore(commit) is True
    assert not (repo / "link_dir").is_symlink()  # the extra symlink is gone
    assert not (repo / "link_dir").exists()
    assert target.is_dir()  # only the link was removed, never its target


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


# ---------------------------------------------------------------------------
# Clean-HEAD fast-path reuse + its guards (fingerprint, skip-worktree)
# ---------------------------------------------------------------------------


def test_clean_head_reflects_working_tree_state(tmp_path: Path):
    """_clean_head returns HEAD's sha for a clean tree (content pinned by HEAD)
    and None for a dirty tree (content not pinned) — the gate that decides
    whether the fast path may reuse a prior snapshot."""
    repo = _init_repo(tmp_path)
    snap = GitSnapshotter(repo)
    head = _git(repo, "rev-parse", "HEAD")
    assert snap._clean_head() == head
    (repo / "a.txt").write_text("dirty\n")
    assert snap._clean_head() is None


def test_index_hides_changes_detects_skip_worktree_and_assume_unchanged(tmp_path: Path):
    """_index_hides_changes flags the skip-worktree / assume-unchanged bits, which
    suppress a file's on-disk change from `git status` — so a clean status would
    NOT imply the tree equals HEAD, and the fast path must be refused."""
    repo = _init_repo(tmp_path)
    snap = GitSnapshotter(repo)
    assert snap._index_hides_changes() is False

    _git(repo, "update-index", "--skip-worktree", "a.txt")
    assert snap._index_hides_changes() is True
    assert snap._clean_head() is None  # bit set -> _clean_head refuses to arm reuse

    _git(repo, "update-index", "--no-skip-worktree", "a.txt")
    _git(repo, "update-index", "--assume-unchanged", "a.txt")
    assert snap._index_hides_changes() is True


def test_capture_fast_path_reuses_commit_on_clean_repeat(tmp_path: Path):
    """Two clean captures at the same HEAD have byte-identical content, so the
    second re-points its ref at the first's commit instead of restaging the tree."""
    repo = _init_repo(tmp_path)
    snap = GitSnapshotter(repo)
    c1 = snap.capture("refs/marim/checkpoints/s/0", "cp0")
    c2 = snap.capture("refs/marim/checkpoints/s/1", "cp1")
    assert c1 and c1 == c2  # reused, not re-captured
    # Both refs resolve to the shared commit, keeping it reachable.
    assert _git(repo, "rev-parse", "refs/marim/checkpoints/s/1") == c1


def test_capture_fast_path_recovers_when_cached_commit_pruned(tmp_path: Path):
    """If the cached fast-path commit is pruned out from under us (gc/repack),
    update-ref fails — capture must clear the stale fingerprint and fall through
    to a full capture, returning a fresh commit rather than None (which would go
    silently dead: checkpoints/file-rewind disabled for the rest of the session)."""
    repo = _init_repo(tmp_path)
    snap = GitSnapshotter(repo)
    c1 = snap.capture("refs/marim/checkpoints/s/0", "cp0")
    assert c1
    # A second clean capture at the same HEAD would reuse c1. Simulate that cached
    # commit having been pruned by pointing the fingerprint at a missing object.
    # (A well-formed but nonexistent sha — not the all-zero null OID, which git
    # treats specially — so update-ref genuinely fails.)
    snap._last_commit = "1" * 40
    c2 = snap.capture("refs/marim/checkpoints/s/1", "cp1")
    assert c2 is not None and c2 != "1" * 40  # a real, fresh full capture
    assert _git(repo, "rev-parse", "refs/marim/checkpoints/s/1") == c2
    # The fingerprint was rearmed by the healing full capture.
    assert snap._last_commit == c2


def test_dirty_capture_disables_fast_path_reuse(tmp_path: Path):
    """A dirty capture must NOT arm the clean fast-path fingerprint: its content
    isn't pinned by HEAD alone, so a later capture can't be allowed to reuse it."""
    repo = _init_repo(tmp_path)
    snap = GitSnapshotter(repo)
    c_clean = snap.capture("refs/marim/checkpoints/s/0", "cp0")
    assert snap._last_clean_head is not None and snap._last_commit == c_clean

    (repo / "a.txt").write_text("dirty\n")
    c_dirty = snap.capture("refs/marim/checkpoints/s/1", "cp1")
    assert c_dirty and c_dirty != c_clean
    assert snap._last_clean_head is None  # reuse disarmed


def test_skip_worktree_change_forces_full_capture_with_live_content(tmp_path: Path):
    """A skip-worktree file hides its on-disk change from `git status`, so a clean
    status would wrongly imply the tree equals HEAD. The guard must force a full
    capture that stages the file's LIVE content, never reuse a stale prior commit —
    otherwise a rewind would silently restore old data."""
    repo = _init_repo(tmp_path)
    snap = GitSnapshotter(repo)
    c1 = snap.capture("refs/marim/checkpoints/s/0", "cp0")  # arms the fast path
    assert c1

    _git(repo, "update-index", "--skip-worktree", "a.txt")
    (repo / "a.txt").write_text("secretly changed\n")
    c2 = snap.capture("refs/marim/checkpoints/s/1", "cp1")
    assert c2 and c2 != c1  # not reused: a fresh full capture
    assert _git(repo, "show", f"{c2}:a.txt") == "secretly changed"  # _git strips
