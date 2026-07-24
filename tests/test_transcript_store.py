from pathlib import Path

from pydantic_ai.messages import ModelRequest, ModelResponse, TextPart, ToolCallPart, UserPromptPart

from marim_harness.session import TranscriptStore


def _msgs():
    return [ModelResponse(parts=[
        TextPart(content="working"),
        ToolCallPart(tool_name="read_file", args={"path": "x"}, tool_call_id="c1"),
    ])]


def _store(tmp_path: Path) -> TranscriptStore:
    return TranscriptStore(tmp_path / "sessions" / "abc.json", "abc")


def test_write_then_read_roundtrips(tmp_path):
    s = _store(tmp_path)
    s.write("toolu_99", _msgs(), cap=2000)
    loaded = s.read("toolu_99")
    assert loaded is not None
    assert loaded[0].parts[0].content == "working"
    assert loaded[0].parts[1].tool_name == "read_file"


def test_read_missing_returns_none(tmp_path):
    assert _store(tmp_path).read("nope") is None


def test_write_sanitizes_id_into_filename(tmp_path):
    s = _store(tmp_path)
    s.write("call/abc.123:x", _msgs(), cap=2000)
    files = list((tmp_path / "sessions" / "abc.subagents").glob("*.json"))
    assert len(files) == 1
    assert all(c.isalnum() or c in "-_." for c in files[0].name)


def test_delete_all_removes_dir(tmp_path):
    s = _store(tmp_path)
    s.write("a", _msgs(), cap=2000)
    assert (tmp_path / "sessions" / "abc.subagents").exists()
    s.delete_all()
    assert not (tmp_path / "sessions" / "abc.subagents").exists()


def test_read_corrupt_returns_none(tmp_path):
    s = _store(tmp_path)
    d = tmp_path / "sessions" / "abc.subagents"
    d.mkdir(parents=True)
    (d / "bad.json").write_text("{not json")
    assert s.read("bad") is None


def _msgs_v2():
    return [ModelRequest(parts=[UserPromptPart(content="hi")])]


def _meta(sid: str, status: str = "running") -> dict:
    return {"stream_id": sid, "type": "general", "task": "t", "status": status,
            "model": None, "mcp": None, "depth": 1, "max_output_chars": None,
            "isolation": None}


def test_v2_envelope_round_trip(tmp_path):
    ts = TranscriptStore(tmp_path / "s.json", "sid")
    ts.write("sg1", _msgs_v2(), 2000, meta=_meta("sg1"))
    assert ts.read("sg1") is not None            # messages come back
    meta = ts.read_meta("sg1")
    assert meta is not None and meta["status"] == "running"


def test_v1_bare_list_still_reads_and_has_no_meta(tmp_path):
    ts = TranscriptStore(tmp_path / "s.json", "sid")
    ts.write("sg1", _msgs_v2(), 2000)               # no meta → v1 bare list on disk
    import json
    files = list(ts._dir.glob("*.json"))
    assert len(files) == 1
    raw = json.loads(files[0].read_text())
    assert isinstance(raw, list)                 # on-disk format unchanged for v1
    assert ts.read("sg1") is not None
    assert ts.read_meta("sg1") is None


def test_safe_filename_is_injective(tmp_path):
    """Distinct stream_ids that sanitize to the same readable stem must still map
    to distinct sidecar files — the appended hash of the true id guarantees it.
    Without it, ``a/b`` and ``a b`` both collapse to ``t-a-b.json`` and one
    spawn's transcript silently overwrites the other's."""
    from marim_harness.session.transcripts import _safe

    assert _safe("a/b") != _safe("a b")  # sanitized stems alone would collide
    ts = TranscriptStore(tmp_path / "s.json", "sid")
    ts.write("a/b", _msgs_v2(), 2000, meta=_meta("a/b"))
    ts.write("a b", _msgs_v2(), 2000, meta=_meta("a b"))
    files = list(ts._dir.glob("*.json"))
    assert len(files) == 2  # two files, neither clobbered the other
    assert ts.read("a/b") is not None
    assert ts.read("a b") is not None
    assert set(ts.scan_meta()) == {"a/b", "a b"}


def test_scan_meta_maps_stream_ids_and_skips_junk(tmp_path):
    ts = TranscriptStore(tmp_path / "s.json", "sid")
    ts.write("sg1", _msgs_v2(), 2000, meta=_meta("sg1"))
    ts.write("sg2", _msgs_v2(), 2000, meta=_meta("sg2", "finished"))
    ts.write("sg3", _msgs_v2(), 2000)               # v1: no meta → not scanned
    (ts._dir / "t-corrupt.json").write_text("{not json")
    metas = ts.scan_meta()
    assert set(metas) == {"sg1", "sg2"}
    assert metas["sg1"]["status"] == "running"


def test_write_stamps_updated_timestamp(tmp_path):
    ts = TranscriptStore(tmp_path / "s.json", "sid")
    ts.write("sg1", _msgs_v2(), 2000, meta=_meta("sg1"))
    assert ts.read_meta("sg1")["updated"]        # non-empty ISO stamp


def test_has_transcript_true_for_v1_and_v2_false_for_missing(tmp_path):
    """``has_transcript`` answers "did this spawn leave ANY sidecar" — the settle
    join uses it to tell a legacy v1 (pre-envelope) spawn that ran to completion
    apart from a spawn that never executed at all (no file)."""
    ts = TranscriptStore(tmp_path / "s.json", "sid")
    ts.write("sg-v1", _msgs_v2(), 2000)                       # v1 bare list
    ts.write("sg-v2", _msgs_v2(), 2000, meta=_meta("sg-v2"))  # v2 envelope
    assert ts.has_transcript("sg-v1")
    assert ts.has_transcript("sg-v2")
    assert not ts.has_transcript("sg-none")


# ---------------------------------------------------------------------------
# image externalization: sidecars must not carry inline base64
# ---------------------------------------------------------------------------


def _img_msgs():
    from pydantic_ai.messages import BinaryContent, ToolReturnPart

    img = BinaryContent(data=b"\x89PNG\r\n\x1a\n" + b"p" * 4096, media_type="image/png")
    return [ModelRequest(parts=[
        ToolReturnPart(tool_name="read_file", content=img, tool_call_id="c9"),
    ])]


def test_write_externalizes_image_bytes_to_cache(tmp_path, monkeypatch):
    """The mid-run checkpoint rewrites a spawn's sidecar before every model
    request — an inline base64 image body there would be re-serialized each
    time. The sidecar must hold a cache ref instead, and read() must
    rehydrate it back to real bytes."""
    monkeypatch.setenv("MARIM_IMAGE_CACHE_DIR", str(tmp_path / "cache"))
    ts = TranscriptStore(tmp_path / "s.json", "sid")
    ts.write("sg-img", _img_msgs(), 2000, meta=_meta("sg-img"))
    files = list((tmp_path / "sid.subagents").glob("*.json"))
    assert len(files) == 1
    raw = files[0].read_text()
    assert "marim-image-cache://" in raw
    assert "p" * 100 not in raw          # no trace of the base64 body
    loaded = ts.read("sg-img")
    assert loaded is not None
    content = loaded[0].parts[0].content
    assert content.data == b"\x89PNG\r\n\x1a\n" + b"p" * 4096
    assert content.media_type == "image/png"


def test_read_degrades_missing_cached_image_to_placeholder(tmp_path, monkeypatch):
    import shutil as _shutil

    monkeypatch.setenv("MARIM_IMAGE_CACHE_DIR", str(tmp_path / "cache"))
    ts = TranscriptStore(tmp_path / "s.json", "sid")
    ts.write("sg-img", _img_msgs(), 2000, meta=_meta("sg-img"))
    _shutil.rmtree(tmp_path / "cache")
    loaded = ts.read("sg-img")
    assert loaded is not None
    assert loaded[0].parts[0].content == "[image unavailable]"
