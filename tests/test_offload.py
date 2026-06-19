# tests/test_offload.py
from pathlib import Path

from marim_harness.tools import offload


def test_small_content_returned_inline(tmp_path: Path):
    assert offload.offload_if_large("hello", kind="grep", key="x",
                                    workspace_root=tmp_path) == "hello"


def test_large_content_offloaded_to_file(tmp_path: Path, monkeypatch):
    monkeypatch.setattr(offload, "_INLINE_CHAR_LIMIT", 10)
    content = "\n".join(f"line {i}" for i in range(50))
    out = offload.offload_if_large(content, kind="grep", key="pat",
                                   workspace_root=tmp_path)
    # handle, not the raw body
    assert "full output saved to" in out
    assert "grep result" in out
    # the file holds the COMPLETE content
    files = list((tmp_path / ".marim" / "output").glob("grep-*.txt"))
    assert len(files) == 1
    assert files[0].read_text() == content
    # preview shows the first lines
    assert "line 0" in out


def test_digest_is_stable_for_same_kind_and_key(tmp_path: Path, monkeypatch):
    monkeypatch.setattr(offload, "_INLINE_CHAR_LIMIT", 1)
    a = offload.offload_if_large("aaa", kind="grep", key="same", workspace_root=tmp_path)
    b = offload.offload_if_large("bbb", kind="grep", key="same", workspace_root=tmp_path)
    # same (kind,key) -> same file path in both handles
    import re
    pa = re.search(r"`([^`]+)`", a).group(1)
    pb = re.search(r"`([^`]+)`", b).group(1)
    assert pa == pb


def test_capped_note_present(tmp_path: Path, monkeypatch):
    monkeypatch.setattr(offload, "_INLINE_CHAR_LIMIT", 1)
    out = offload.offload_if_large("data", kind="tree", key="k",
                                   workspace_root=tmp_path, capped=True)
    assert "ceiling" in out.lower()


def test_no_workspace_clips_instead_of_offloading(monkeypatch):
    monkeypatch.setattr(offload, "_INLINE_CHAR_LIMIT", 10)
    content = "x" * 200
    out = offload.offload_if_large(content, kind="glob", key="k", workspace_root=None)
    assert "saved to" not in out
    assert len(out) < 200
    assert "clipped" in out.lower()
