from marim_harness.interfaces.tui.widgets.tool_summary import (
    ToolSummary,
    _clip_middle,
    humanize_tool,
    summarize,
)


def test_single_arg_tool_targets_its_value():
    s = summarize("read_file", {"path": ".marim/test_output.txt"})
    assert s == ToolSummary(label="Read", target=".marim/test_output.txt", badges=())


def test_spawn_agent_preview_prefers_description():
    s = summarize("spawn_agent", {
        "type": "explore", "task": "a very long task body that we don't want shown",
        "description": "review core loop", "background": True,
    })
    assert s.label == "Spawn Agent"
    assert s.target == "review core loop"


def test_spawn_agent_preview_falls_back_to_task_never_a_bare_bool():
    # Regression: with no `description` and an early boolean arg, the old
    # "first meaningful arg" fallback surfaced "True" instead of the task.
    s = summarize("spawn_agent", {"background": True, "type": "explore",
                                   "task": "review the parser"})
    assert s.target == "review the parser"
    assert s.target != "True"


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


def test_update_tasks_digests_instead_of_dumping_the_list():
    # The old code rendered this as [{'text': 'a', 'status': 'done'}, …].
    s = summarize(
        "update_tasks",
        {
            "todos": [
                {"text": "Run static analysis", "status": "done"},
                {"text": "Map project structure", "status": "in_progress"},
                {"text": "Write report", "status": "pending"},
            ]
        },
    )
    assert s.label == "Update Tasks"
    assert s.target == "1/3 done · ▸ Map project structure"
    assert "{" not in s.target


def test_update_tasks_without_in_progress_shows_counts_only():
    s = summarize(
        "update_tasks",
        {"todos": [{"text": "a", "status": "done"}, {"text": "b", "status": "done"}]},
    )
    assert s.target == "2/2 done"


def test_update_tasks_empty_is_label_only():
    s = summarize("update_tasks", {"todos": []})
    assert s == ToolSummary(label="Update Tasks", target="", badges=())


def test_remember_targets_title_regardless_of_arg_order():
    # The model may emit `body` first; the header must still show the title, not
    # a chunk of the multi-line memory body.
    args = {
        "body": "## Detail\nlots of text\nmore text",
        "title": "User prefers uv",
        "description": "The user prefers uv for Python envs.",
        "scope": "project",
        "type": "user",
    }
    s = summarize("remember", args)
    assert s.label == "Remember"
    assert s.target == "User prefers uv"
    assert s.badges == ()  # project scope is the silent default


def test_remember_global_scope_becomes_a_badge():
    s = summarize(
        "remember",
        {"title": "User's name is Mateus", "body": "x", "scope": "global"},
    )
    assert s.target == "User's name is Mateus"
    assert s.badges == ("global",)


def test_recall_targets_name_and_flags_global_scope():
    s = summarize("recall", {"name": "User's name is Mateus", "scope": "global"})
    assert s.label == "Recall"
    assert s.target == "User's name is Mateus"
    assert s.badges == ("global",)


def test_humanize_tool_maps_known_and_titlecases_unknown():
    assert humanize_tool("read_file") == "Read"
    assert humanize_tool("spawn_agent") == "Spawn Agent"


def test_clip_middle_noop_when_short():
    assert _clip_middle("short", 30) == "short"
