from marim_harness.session.checkpoints import Checkpoint, NullSnapshotter


def test_checkpoint_roundtrips_through_dict():
    cp = Checkpoint(
        index=2, history_len=6, commit="abc123",
        created="2026-06-23T00:00:00+00:00", prompt_preview="fix the bug",
    )
    assert Checkpoint.from_dict(cp.to_dict()) == cp


def test_from_dict_tolerates_missing_optional_commit():
    d = {"index": 0, "history_len": 0, "created": "t", "prompt_preview": ""}
    assert Checkpoint.from_dict(d).commit is None


def test_null_snapshotter_captures_nothing():
    assert NullSnapshotter().capture("refs/x", "msg") is None
