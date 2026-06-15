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


def test_edit_file_replaces_unique_match(tmp_path: Path):
    (tmp_path / "a.txt").write_text("foo bar foo-baz")
    fs.edit_file(tmp_path, "a.txt", "foo-baz", "qux")
    assert (tmp_path / "a.txt").read_text() == "foo bar qux"


def test_edit_file_no_match_raises(tmp_path: Path):
    (tmp_path / "a.txt").write_text("foo")
    with pytest.raises(ModelRetry):
        fs.edit_file(tmp_path, "a.txt", "missing", "x")


def test_edit_file_multiple_matches_raises(tmp_path: Path):
    (tmp_path / "a.txt").write_text("foo foo")
    with pytest.raises(ModelRetry):
        fs.edit_file(tmp_path, "a.txt", "foo", "x")


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


def test_path_escape_raises_model_retry(tmp_path: Path):
    with pytest.raises(ModelRetry):
        fs.read_file(tmp_path, "../escape.txt")
