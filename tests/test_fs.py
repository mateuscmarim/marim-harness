import os
from pathlib import Path

import pytest
from pydantic_ai import ModelRetry

from marim_harness.tools import fs


def test_read_file_adds_line_numbers(tmp_path: Path):
    (tmp_path / "a.txt").write_text("foo\nbar")
    out = fs.read_file(tmp_path, "a.txt")
    assert out == "1\tfoo\n2\tbar"


def test_read_missing_file_raises_model_retry(tmp_path: Path):
    with pytest.raises(ModelRetry):
        fs.read_file(tmp_path, "nope.txt")


def test_read_file_offset_starts_at_line(tmp_path: Path):
    (tmp_path / "a.txt").write_text("a\nb\nc\nd")
    out = fs.read_file(tmp_path, "a.txt", offset=3)
    # Real line numbers are preserved, and a window != whole file gets a footer.
    assert out.startswith("3\tc\n4\td")
    assert "of 4]" in out


def test_read_file_limit_caps_lines(tmp_path: Path):
    (tmp_path / "a.txt").write_text("a\nb\nc\nd\ne")
    out = fs.read_file(tmp_path, "a.txt", limit=2)
    body = out.split("\n\n[")[0]
    assert body == "1\ta\n2\tb"  # only the first two lines
    assert "of 5]" in out  # footer signals more remain


def test_read_file_offset_and_limit_window(tmp_path: Path):
    (tmp_path / "a.txt").write_text("a\nb\nc\nd\ne")
    out = fs.read_file(tmp_path, "a.txt", offset=2, limit=2)
    body = out.split("\n\n[")[0]
    assert body == "2\tb\n3\tc"
    assert "lines 2-3 of 5]" in out


def test_read_file_default_cap_truncates(tmp_path: Path, monkeypatch):
    monkeypatch.setattr(fs, "_DEFAULT_READ_LIMIT", 3)
    (tmp_path / "a.txt").write_text("\n".join(f"l{i}" for i in range(1, 6)))
    out = fs.read_file(tmp_path, "a.txt")  # no explicit limit -> capped at 3
    body = out.split("\n\n[")[0]
    assert body == "1\tl1\n2\tl2\n3\tl3"
    assert "of 5]" in out


def test_read_file_explicit_limit_overrides_default_cap(tmp_path: Path, monkeypatch):
    monkeypatch.setattr(fs, "_DEFAULT_READ_LIMIT", 2)
    (tmp_path / "a.txt").write_text("a\nb\nc\nd")
    # An explicit limit larger than the default cap is honored.
    out = fs.read_file(tmp_path, "a.txt", limit=10)
    assert out == "1\ta\n2\tb\n3\tc\n4\td"  # whole file, no footer


def test_read_file_clips_overlong_lines(tmp_path: Path, monkeypatch):
    monkeypatch.setattr(fs, "_MAX_LINE_CHARS", 10)
    (tmp_path / "a.txt").write_text("x" * 25 + "\nshort")
    out = fs.read_file(tmp_path, "a.txt")
    # The wide line is clipped to the cap with a remainder note; the footer flags it.
    assert "1\t" + "x" * 10 + "… (+15 more chars on this line)" in out
    assert "2\tshort" in out
    assert "long lines clipped to 10 chars]" in out


def test_read_file_stops_at_char_budget(tmp_path: Path, monkeypatch):
    # Budget fits ~2 rows of "N\tdataXXXX" (~10 chars each), so the window ends
    # early even though the line limit would allow all five.
    monkeypatch.setattr(fs, "_MAX_READ_CHARS", 24)
    (tmp_path / "a.txt").write_text("\n".join("data" + str(i) for i in range(1, 6)))
    out = fs.read_file(tmp_path, "a.txt")
    body = out.split("\n\n[")[0]
    last = int(out.split(" of 5]")[0].split("-")[-1])
    assert 1 <= last < 5  # stopped before the end
    assert body.count("\n") + 1 == last  # body holds exactly `last` rows
    assert f"showing lines 1-{last} of 5]" in out


def test_read_file_always_returns_at_least_one_line(tmp_path: Path, monkeypatch):
    # Even with a budget smaller than a single row, one (clipped) row comes back.
    monkeypatch.setattr(fs, "_MAX_READ_CHARS", 1)
    monkeypatch.setattr(fs, "_MAX_LINE_CHARS", 5)
    (tmp_path / "a.txt").write_text("x" * 50 + "\nsecond")
    out = fs.read_file(tmp_path, "a.txt")
    assert out.startswith("1\t" + "x" * 5 + "…")
    assert "2\t" not in out  # the second line was budgeted out
    assert "showing lines 1-1 of 2" in out


def test_read_file_offset_past_eof_raises(tmp_path: Path):
    (tmp_path / "a.txt").write_text("a\nb")
    with pytest.raises(ModelRetry):
        fs.read_file(tmp_path, "a.txt", offset=5)


def test_read_file_rejects_bad_offset_and_limit(tmp_path: Path):
    (tmp_path / "a.txt").write_text("a\nb")
    with pytest.raises(ModelRetry):
        fs.read_file(tmp_path, "a.txt", offset=0)
    with pytest.raises(ModelRetry):
        fs.read_file(tmp_path, "a.txt", limit=0)


def test_write_file_creates_parents(tmp_path: Path):
    fs.write_file(tmp_path, "sub/a.txt", "hello")
    assert (tmp_path / "sub/a.txt").read_text() == "hello"


def _edit(old, new, replace_all=False) -> fs.Edit:
    return fs.Edit(old_string=old, new_string=new, replace_all=replace_all)


def test_edit_file_single_replaces_unique_match(tmp_path: Path):
    (tmp_path / "a.txt").write_text("foo bar foo-baz")
    fs.edit_file(tmp_path, "a.txt", [_edit("foo-baz", "qux")])
    assert (tmp_path / "a.txt").read_text() == "foo bar qux"


def test_edit_file_no_match_raises(tmp_path: Path):
    (tmp_path / "a.txt").write_text("foo")
    with pytest.raises(ModelRetry):
        fs.edit_file(tmp_path, "a.txt", [_edit("missing", "x")])


def test_edit_file_rejects_non_utf8_file(tmp_path: Path):
    """A non-UTF-8 file can't be safely round-tripped (decode-replace would
    corrupt the undecodable bytes on write-back), so edit_file refuses with a
    ModelRetry instead of crashing with UnicodeDecodeError or mangling the file."""
    p = tmp_path / "latin1.txt"
    p.write_bytes(b"caf\xe9 foo")  # 0xe9 is 'é' in latin-1, invalid UTF-8
    with pytest.raises(ModelRetry):
        fs.edit_file(tmp_path, "latin1.txt", [_edit("foo", "bar")])
    # The original bytes are untouched — no partial/corrupting write happened.
    assert p.read_bytes() == b"caf\xe9 foo"


def test_edit_file_multiple_matches_raises(tmp_path: Path):
    (tmp_path / "a.txt").write_text("foo foo")
    with pytest.raises(ModelRetry):
        fs.edit_file(tmp_path, "a.txt", [_edit("foo", "x")])


def test_edit_file_applies_edits_sequentially(tmp_path: Path):
    (tmp_path / "a.txt").write_text("alpha beta")
    # The second edit matches text produced by the first.
    fs.edit_file(tmp_path, "a.txt", [_edit("alpha", "gamma"), _edit("gamma beta", "done")])
    assert (tmp_path / "a.txt").read_text() == "done"


def test_edit_file_is_atomic_on_later_failure(tmp_path: Path):
    (tmp_path / "a.txt").write_text("keep this")
    with pytest.raises(ModelRetry) as exc:
        fs.edit_file(
            tmp_path, "a.txt",
            [_edit("keep", "kept"), _edit("nonexistent", "x")],
        )
    assert "edit 2" in str(exc.value)  # failure names the offending edit
    assert (tmp_path / "a.txt").read_text() == "keep this"  # nothing written


def test_edit_file_replace_all(tmp_path: Path):
    (tmp_path / "a.txt").write_text("x x x")
    fs.edit_file(tmp_path, "a.txt", [_edit("x", "y", replace_all=True)])
    assert (tmp_path / "a.txt").read_text() == "y y y"


def test_edit_file_empty_edits_raises(tmp_path: Path):
    (tmp_path / "a.txt").write_text("foo")
    with pytest.raises(ModelRetry):
        fs.edit_file(tmp_path, "a.txt", [])


def test_glob_lists_matching_files(tmp_path: Path):
    (tmp_path / "a.py").write_text("")
    (tmp_path / "b.txt").write_text("")
    assert fs.glob_files(tmp_path, "*.py") == "a.py"


def test_grep_returns_location_lines(tmp_path: Path):
    (tmp_path / "a.txt").write_text("alpha\nbeta\nalpha2")
    out = fs.grep(tmp_path, "alpha")
    assert "a.txt:1:alpha" in out
    assert "a.txt:3:alpha2" in out
    assert "beta" not in out


def test_grep_does_not_follow_symlink_out_of_workspace(tmp_path: Path):
    root = tmp_path / "ws"
    root.mkdir()
    secret = tmp_path / "secret.txt"
    secret.write_text("top secret token")
    (root / "leak.txt").symlink_to(secret)
    out = fs.grep(root, "secret")
    assert "top secret token" not in out
    assert out == "(no matches)"


def test_path_escape_raises_model_retry(tmp_path: Path):
    with pytest.raises(ModelRetry):
        fs.read_file(tmp_path, "../escape.txt")


def test_glob_escaping_pattern_excludes_outside_files(tmp_path: Path):
    root = tmp_path / "ws"
    root.mkdir()
    secret = tmp_path / "secret"
    secret.mkdir()
    (secret / "key.txt").write_text("top secret")
    out = fs.glob_files(root, "../secret/*")
    assert "key.txt" not in out
    assert out == "(no matches)"


def test_glob_absolute_pattern_raises_model_retry(tmp_path: Path):
    with pytest.raises(ModelRetry):
        fs.glob_files(tmp_path, "/etc/hostname")


def test_glob_valid_pattern_returns_relative_paths(tmp_path: Path):
    (tmp_path / "a.py").write_text("")
    (tmp_path / "sub").mkdir()
    (tmp_path / "sub" / "b.py").write_text("")
    out = fs.glob_files(tmp_path, "**/*.py")
    assert out == "a.py\nsub/b.py"


def test_tree_depth_one_is_a_flat_listing(tmp_path: Path):
    (tmp_path / "b.txt").write_text("")
    (tmp_path / "a.txt").write_text("")
    (tmp_path / "sub").mkdir()
    out = fs.tree(tmp_path, ".", depth=1)
    # dirs first (with trailing slash), then files, each sorted; not recursive
    assert out == "sub/\na.txt\nb.txt"


def test_tree_recurses_and_indents(tmp_path: Path):
    (tmp_path / "sub").mkdir()
    (tmp_path / "sub" / "b.py").write_text("")
    (tmp_path / "a.py").write_text("")
    out = fs.tree(tmp_path, ".", depth=2)
    lines = out.splitlines()
    assert lines[0] == "sub/"
    assert "  b.py" in lines  # nested entry indented two spaces
    assert "a.py" in lines


def test_tree_skips_noise_dirs_but_lists_them(tmp_path: Path):
    (tmp_path / "node_modules").mkdir()
    (tmp_path / "node_modules" / "junk.js").write_text("")
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "main.py").write_text("")
    out = fs.tree(tmp_path, ".", depth=3)
    assert "node_modules/" in out  # the dir itself is shown
    assert "junk.js" not in out  # but its contents are not expanded
    assert "main.py" in out  # ordinary dirs still descend


def test_tree_shows_dotfiles(tmp_path: Path):
    (tmp_path / ".env").write_text("")
    out = fs.tree(tmp_path, ".", depth=1)
    assert ".env" in out


def test_tree_on_subpath(tmp_path: Path):
    (tmp_path / "sub").mkdir()
    (tmp_path / "sub" / "x.txt").write_text("")
    out = fs.tree(tmp_path, "sub", depth=1)
    assert out == "x.txt"


def test_tree_non_directory_raises(tmp_path: Path):
    (tmp_path / "a.txt").write_text("")
    with pytest.raises(ModelRetry):
        fs.tree(tmp_path, "a.txt", depth=1)


def test_tree_escape_raises(tmp_path: Path):
    with pytest.raises(ModelRetry):
        fs.tree(tmp_path, "..", depth=1)


def test_tree_empty_dir(tmp_path: Path):
    assert fs.tree(tmp_path, ".", depth=1) == "(empty)"



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


def test_grep_offloads_large_result(tmp_path, monkeypatch):
    from marim_harness.tools import offload
    monkeypatch.setattr(offload, "_INLINE_CHAR_LIMIT", 50)
    (tmp_path / "big.txt").write_text("\n".join(f"match {i}" for i in range(100)))
    out = fs.grep(tmp_path, "match")
    assert "full output saved to" in out and "grep result" in out
    saved = list((tmp_path / ".marim" / "output").glob("grep-*.txt"))
    assert len(saved) == 1
    # every hit is in the file, nothing truncated
    assert saved[0].read_text().count("big.txt:") == 100
    assert "(truncated)" not in out


def test_grep_small_result_still_inline(tmp_path):
    (tmp_path / "a.txt").write_text("alpha\nbeta")
    out = fs.grep(tmp_path, "alpha")
    assert out == "a.txt:1:alpha"


def test_glob_offloads_large_result(tmp_path, monkeypatch):
    from marim_harness.tools import offload
    monkeypatch.setattr(offload, "_INLINE_CHAR_LIMIT", 50)
    for i in range(100):
        (tmp_path / f"f{i}.txt").write_text("x")
    out = fs.glob_files(tmp_path, "*.txt")
    assert "full output saved to" in out and "glob result" in out
    saved = list((tmp_path / ".marim" / "output").glob("glob-*.txt"))
    assert len(saved) == 1
    assert saved[0].read_text().count(".txt") == 100


def test_tree_offloads_large_listing(tmp_path, monkeypatch):
    from marim_harness.tools import offload
    monkeypatch.setattr(offload, "_INLINE_CHAR_LIMIT", 50)
    for i in range(100):
        (tmp_path / f"f{i:03d}.txt").write_text("x")
    out = fs.tree(tmp_path, ".", depth=1)
    assert "full output saved to" in out and "tree result" in out
    saved = list((tmp_path / ".marim" / "output").glob("tree-*.txt"))
    assert len(saved) == 1
    assert saved[0].read_text().count(".txt") == 100
    assert "(truncated)" not in out


def test_grep_skips_noise_dirs(tmp_path: Path):
    (tmp_path / "a.txt").write_text("needle here")
    (tmp_path / "node_modules" / "pkg").mkdir(parents=True)
    (tmp_path / "node_modules" / "pkg" / "x.js").write_text("needle in node_modules")
    (tmp_path / ".git").mkdir()
    (tmp_path / ".git" / "config").write_text("needle in git")
    out = fs.grep(tmp_path, "needle")
    assert "a.txt:1:needle here" in out
    assert "node_modules" not in out
    assert ".git" not in out


def test_grep_searches_non_noise_dotfile_dir(tmp_path: Path):
    (tmp_path / ".github").mkdir()
    (tmp_path / ".github" / "ci.yml").write_text("needle in ci")
    out = fs.grep(tmp_path, "needle")
    assert ".github/ci.yml:1:needle in ci" in out


def test_grep_skips_binary_files(tmp_path: Path):
    # A NUL byte marks the file binary; its "needle" must not be returned.
    (tmp_path / "blob.bin").write_bytes(b"\x00needle\x00binary")
    (tmp_path / "code.txt").write_text("needle in text")
    out = fs.grep(tmp_path, "needle")
    assert "code.txt:1:needle in text" in out
    assert "blob.bin" not in out


def test_grep_finds_deeply_nested_match(tmp_path: Path):
    deep = tmp_path / "a" / "b" / "c"
    deep.mkdir(parents=True)
    (deep / "d.txt").write_text("first\nneedle on line two")
    out = fs.grep(tmp_path, "needle")
    assert "a/b/c/d.txt:2:needle on line two" in out


def test_grep_skips_unreadable_directories(tmp_path: Path, monkeypatch):
    """A directory the process can't read (PermissionError from os.walk)
    must not crash grep — it's skipped and other files are still found."""
    (tmp_path / "good.txt").write_text("needle here")
    bad_dir = tmp_path / "noperm"
    bad_dir.mkdir()
    (bad_dir / "secret.txt").write_text("secret needle")
    # Simulate os.walk raising PermissionError on the bad dir (we run as root
    # so real chmod can't trigger this).  The original os.walk yields (dir,dirs,files)
    # tuples; we intercept and raise for the noperm path.
    _real_walk = os.walk

    def _patched_walk(top, *a, **kw):
        for dirpath, dirnames, filenames in _real_walk(top, *a, **kw):
            if dirpath == str(bad_dir):
                raise PermissionError(f"permission denied: {dirpath}")
            yield dirpath, dirnames, filenames

    monkeypatch.setattr(fs.os, "walk", _patched_walk)
    out = fs.grep(tmp_path, "needle")
    assert "good.txt:1:needle here" in out
    assert "secret.txt" not in out


def test_tree_skips_unreadable_directories(tmp_path: Path, monkeypatch):
    """A directory the process can't read must not crash tree."""
    (tmp_path / "visible.txt").write_text("")
    bad_dir = tmp_path / "noperm"
    bad_dir.mkdir()
    (bad_dir / "hidden.txt").write_text("")
    # _walk_tree uses directory.iterdir(), not os.walk.  Make iterdir raise
    # PermissionError on the bad dir to exercise the error path.
    real_iterdir = Path.iterdir

    def _patched_iterdir(self):
        if self == bad_dir:
            raise PermissionError(f"permission denied: {self}")
        return real_iterdir(self)

    monkeypatch.setattr(Path, "iterdir", _patched_iterdir)
    out = fs.tree(tmp_path, ".", depth=2)
    assert "visible.txt" in out
    assert "hidden.txt" not in out
