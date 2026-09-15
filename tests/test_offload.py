"""Compatibility checks for absolute pointers in persisted histories."""

from marim_harness.tools.impl import offload


def test_find_offload_paths_extracts_backticked_path():
    text = "blah ⚠️ Large bash result — full output saved to `/tmp/pad/bash-abc.txt`. Read more"
    assert offload.find_offload_paths(text) == ["/tmp/pad/bash-abc.txt"]


def test_find_offload_paths_none_on_plain_text():
    assert offload.find_offload_paths("ordinary output, nothing offloaded") == []
    # An elided-pointer placeholder is NOT a handle.
    assert (
        offload.find_offload_paths(
            "[output elided to save context; full content at /pad/x — read_file it if still needed]"
        )
        == []
    )
