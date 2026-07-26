# tests/test_offload.py
from pathlib import Path

from marim_harness.tools.impl import offload


def test_small_content_returned_inline(tmp_path: Path):
    assert offload.offload_if_large("hello", kind="grep", key="x",
                                    offload_dir=tmp_path) == "hello"


def test_large_content_offloaded_to_file(tmp_path: Path, monkeypatch):
    monkeypatch.setattr(offload, "_INLINE_CHAR_LIMIT", 10)
    content = "\n".join(f"line {i}" for i in range(50))
    out = offload.offload_if_large(content, kind="grep", key="pat",
                                   offload_dir=tmp_path)
    assert "full output saved to" in out
    assert "grep result" in out
    # the file holds the COMPLETE content — flat in the offload dir
    files = list(tmp_path.glob("grep-*.txt"))
    assert len(files) == 1
    assert files[0].read_text() == content
    # handle shows absolute path
    assert tmp_path.as_posix() in out
    # preview shows the first lines
    assert "line 0" in out


def test_digest_is_stable_for_same_kind_and_key(tmp_path: Path, monkeypatch):
    monkeypatch.setattr(offload, "_INLINE_CHAR_LIMIT", 1)
    a = offload.offload_if_large("aaa", kind="grep", key="same",
                                 offload_dir=tmp_path)
    b = offload.offload_if_large("bbb", kind="grep", key="same",
                                 offload_dir=tmp_path)
    import re
    pa = re.search(r"`([^`]+)`", a).group(1)
    pb = re.search(r"`([^`]+)`", b).group(1)
    assert pa == pb


def test_capped_note_present(tmp_path: Path, monkeypatch):
    monkeypatch.setattr(offload, "_INLINE_CHAR_LIMIT", 1)
    out = offload.offload_if_large("data", kind="tree", key="k",
                                   offload_dir=tmp_path, capped=True)
    assert "ceiling" in out.lower()


def test_no_offload_dir_clips_instead_of_offloading(monkeypatch):
    monkeypatch.setattr(offload, "_INLINE_CHAR_LIMIT", 10)
    content = "x" * 200
    out = offload.offload_if_large(content, kind="glob", key="k",
                                   offload_dir=None)
    assert "saved to" not in out
    assert len(out) < 200
    assert "clipped" in out.lower()


def test_write_handle_goes_through_atomic_layer(tmp_path: Path, monkeypatch):
    calls: list[tuple[Path, str]] = []
    real = offload.atomic_write_text

    def _spy(path, text, **kw):
        calls.append((Path(path), text))
        return real(path, text, **kw)

    monkeypatch.setattr(offload, "atomic_write_text", _spy)
    content = "exact content\nsecond line\n"
    out = offload._write_handle(content, kind="grep", key="k",
                                offload_dir=tmp_path, capped=False)
    assert calls, "atomic_write_text was not used"
    written_path, written_text = calls[0]
    assert written_text == content
    assert written_path.read_text() == content
    assert "full output saved to" in out
    # absolute path in handle
    assert tmp_path.as_posix() in out


def test_write_preview_file_goes_through_atomic_layer(tmp_path: Path, monkeypatch):
    calls: list[tuple[Path, str]] = []
    real = offload.atomic_write_text

    def _spy(path, text, **kw):
        calls.append((Path(path), text))
        return real(path, text, **kw)

    monkeypatch.setattr(offload, "atomic_write_text", _spy)
    content = "preview body line A\nline B\n"
    rel_posix, preview, n_lines = offload.write_preview_file(
        content, filename="abc123.md", offload_dir=tmp_path)
    assert calls, "atomic_write_text was not used"
    assert calls[0][1] == content
    assert (tmp_path / "abc123.md").read_text() == content
    assert rel_posix == (tmp_path / "abc123.md").as_posix()
    assert "line A" in preview


def test_get_offload_dir_prefers_scratchpad(tmp_path):
    ws = tmp_path / "workspace"
    scratch = tmp_path / "scratchpad"
    assert offload.get_offload_dir(ws, scratch) == scratch


def test_get_offload_dir_falls_back_to_workspace(tmp_path):
    ws = tmp_path / "workspace"
    assert offload.get_offload_dir(ws, None) == ws


def test_get_offload_dir_returns_none_when_both_none():
    assert offload.get_offload_dir(None, None) is None


# --- offload-handle envelope (spec 2026-07-26) --------------------------------


def test_find_offload_paths_extracts_backticked_path():
    text = "blah ⚠️ Large bash result — full output saved to `/tmp/pad/bash-abc.txt`. Read more"
    assert offload.find_offload_paths(text) == ["/tmp/pad/bash-abc.txt"]


def test_find_offload_paths_none_on_plain_text():
    assert offload.find_offload_paths("ordinary output, nothing offloaded") == []
    # An elided-pointer placeholder is NOT a handle.
    assert offload.find_offload_paths(
        "[output elided to save context; full content at /pad/x — read_file it if still needed]"
    ) == []


def test_write_handle_matches_envelope(tmp_path: Path, monkeypatch):
    """Tripwire: a copy edit to _write_handle that breaks the envelope fails here."""
    monkeypatch.setattr(offload, "_INLINE_CHAR_LIMIT", 10)
    result = offload.offload_if_large(
        "line\n" * 50, kind="bash", key="k1", offload_dir=tmp_path
    )
    paths = offload.find_offload_paths(result)
    assert len(paths) == 1
    p = Path(paths[0])
    assert p.is_absolute() and p.exists()
    assert p.read_text() == "line\n" * 50


def test_fetch_offload_matches_envelope(tmp_path: Path):
    """Tripwire: fetch's handle copy stays on the shared envelope."""
    from marim_harness.tools.impl.fetch import _offload

    handle = _offload("# Title\n" + "body\n" * 100, "https://example.com/page", tmp_path)
    paths = offload.find_offload_paths(handle)
    assert len(paths) == 1
    p = Path(paths[0])
    assert p.is_absolute() and p.exists()
