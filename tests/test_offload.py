# tests/test_offload.py
from pathlib import Path

from marim_harness.tools.impl import offload


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


def test_write_handle_goes_through_atomic_layer(tmp_path: Path, monkeypatch):
    """_write_handle must route through atomic_write_text (unique temp + os.replace)
    rather than a direct dest.write_text, so two concurrent writers to the same
    sha-derived path can't clobber each other. Spy on the atomic helper to prove it
    is used, and confirm the on-disk content is exactly what was passed."""
    calls: list[tuple[Path, str]] = []
    real = offload.atomic_write_text

    def _spy(path, text, **kw):
        calls.append((Path(path), text))
        return real(path, text, **kw)

    monkeypatch.setattr(offload, "atomic_write_text", _spy)
    content = "exact content\nsecond line\n"
    out = offload._write_handle(content, kind="grep", key="k",
                                workspace_root=tmp_path, capped=False)
    assert calls, "atomic_write_text was not used"
    written_path, written_text = calls[0]
    assert written_text == content
    assert written_path.read_text() == content
    assert "full output saved to" in out


def test_write_preview_file_goes_through_atomic_layer(tmp_path: Path, monkeypatch):
    """write_preview_file (used by fetch's offload) must also use atomic_write_text,
    and the file must hold the exact content."""
    calls: list[tuple[Path, str]] = []
    real = offload.atomic_write_text

    def _spy(path, text, **kw):
        calls.append((Path(path), text))
        return real(path, text, **kw)

    monkeypatch.setattr(offload, "atomic_write_text", _spy)
    content = "preview body line A\nline B\n"
    rel = Path(".marim", "fetch", "abc123.md")
    rel_posix, preview, n_lines = offload.write_preview_file(
        content, rel=rel, workspace_root=tmp_path)
    assert calls, "atomic_write_text was not used"
    assert calls[0][1] == content
    assert (tmp_path / rel).read_text() == content
    assert rel_posix == rel.as_posix()
    assert "line A" in preview
