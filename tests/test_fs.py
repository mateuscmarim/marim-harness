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


def test_tree_truncates_huge_listings(tmp_path: Path):
    for i in range(fs._MAX_TREE_ENTRIES + 50):
        (tmp_path / f"f{i:04d}.txt").write_text("")
    out = fs.tree(tmp_path, ".", depth=1)
    assert "(truncated)" in out
    assert len(out.splitlines()) == fs._MAX_TREE_ENTRIES + 1  # entries + marker
