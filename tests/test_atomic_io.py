import os
from pathlib import Path

from marim_harness.atomic_io import atomic_write_text


def test_atomic_write_creates_file_with_content(tmp_path: Path):
    p = tmp_path / "a.json"
    atomic_write_text(p, '{"x": 1}')
    assert p.read_text() == '{"x": 1}'


def test_atomic_write_overwrites_existing(tmp_path: Path):
    p = tmp_path / "a.json"
    p.write_text("old")
    atomic_write_text(p, "new")
    assert p.read_text() == "new"


def test_atomic_write_leaves_no_temp_residue(tmp_path: Path):
    # The old code used a deterministic "<name>.json.tmp" that concurrent writers
    # would clobber. The atomic writer must clean up its temp and never leave that
    # shared fixed name behind.
    p = tmp_path / "a.json"
    atomic_write_text(p, "data")
    assert not (tmp_path / "a.json.tmp").exists()
    assert list(tmp_path.iterdir()) == [p]  # only the target, no temp residue


def test_atomic_write_uses_unique_temp_names(tmp_path: Path, monkeypatch):
    # Two writers preparing temp files concurrently must get distinct paths, so
    # one can't truncate the other's half-written temp (the bug behind the shared
    # ".json.tmp"). Capture the temp paths by stubbing os.replace to record them.
    seen: list[str] = []

    def spy_replace(src, dst):
        seen.append(str(src))  # don't perform the swap, so both temps coexist

    monkeypatch.setattr(os, "replace", spy_replace)
    atomic_write_text(tmp_path / "a.json", "1")
    atomic_write_text(tmp_path / "a.json", "2")
    assert len(seen) == 2
    assert seen[0] != seen[1]  # distinct temp files, never a shared fixed name
