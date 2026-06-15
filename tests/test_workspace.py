from pathlib import Path

import pytest

from marim_harness.workspace import WorkspaceError, resolve_in_workspace


def test_resolves_path_inside_workspace(tmp_path: Path):
    resolved = resolve_in_workspace(tmp_path, "sub/file.txt")
    assert resolved == (tmp_path / "sub/file.txt").resolve()


def test_rejects_parent_traversal(tmp_path: Path):
    with pytest.raises(WorkspaceError):
        resolve_in_workspace(tmp_path, "../escape.txt")


def test_rejects_absolute_path_outside(tmp_path: Path):
    with pytest.raises(WorkspaceError):
        resolve_in_workspace(tmp_path, "/etc/passwd")
