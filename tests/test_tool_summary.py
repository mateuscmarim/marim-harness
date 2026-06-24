from marim_harness.interfaces.tui.widgets.tool_summary import (
    ToolSummary,
    _clip_middle,
    humanize_tool,
    summarize,
)


def test_single_arg_tool_targets_its_value():
    s = summarize("read_file", {"path": ".marim/test_output.txt"})
    assert s == ToolSummary(label="Read", target=".marim/test_output.txt", badges=())


def test_multi_arg_tool_uses_registered_target_not_repr():
    # The old code rendered this as wait_for_job(id='job-6', timeout=600).
    s = summarize("wait_for_job", {"id": "job-6", "timeout": 600})
    assert s.label == "Wait"
    assert s.target == "job-6"
    assert s.badges == ()  # timeout is dropped as default-noise


def test_bash_background_becomes_a_badge():
    s = summarize("bash", {"command": "uv run pytest", "background": True})
    assert s.label == "Bash"
    assert s.target == "uv run pytest"
    assert s.badges == ("bg",)


def test_bash_command_clips_middle_keeping_the_tail():
    cmd = "uv run pytest --no-cov -q 2>&1 | grep -E '^[0-9]+ ' | tail -1"
    s = summarize("bash", {"command": cmd}, cap=30)
    assert s.target.startswith("uv run pytest")
    assert s.target.endswith("tail -1")
    assert "…" in s.target and len(s.target) <= 30


def test_grep_path_becomes_an_in_badge():
    s = summarize("grep", {"pattern": "build_harness", "path": "src/"})
    assert s.label == "Grep"
    assert s.target == "build_harness"
    assert s.badges == ("in src/",)


def test_unknown_tool_falls_back_to_humanized_name_and_first_arg():
    s = summarize("frobnicate_thing", {"widget": "gizmo", "level": 9})
    assert s.label == "Frobnicate Thing"
    assert s.target == "gizmo"
    assert s.badges == ()


def test_empty_args_gives_label_only():
    s = summarize("tree", {})
    assert s == ToolSummary(label="Tree", target="", badges=())


def test_humanize_tool_maps_known_and_titlecases_unknown():
    assert humanize_tool("read_file") == "Read"
    assert humanize_tool("spawn_agent") == "Spawn Agent"


def test_clip_middle_noop_when_short():
    assert _clip_middle("short", 30) == "short"
