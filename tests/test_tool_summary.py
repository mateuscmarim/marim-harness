from marim_harness.interfaces.tui.widgets.tool_summary import (
    ToolSummary,
    _clip_middle,
    humanize_tool,
    summarize,
)


def test_single_arg_tool_targets_its_value():
    s = summarize("read_file", {"path": ".marim/test_output.txt"})
    assert s == ToolSummary(label="Read", target=".marim/test_output.txt", badges=())


def test_read_full_file_stays_a_bare_path():
    # No offset/limit -> a full read shows just the path, no range.
    s = summarize("read_file", {"path": "app.py"})
    assert s.target == "app.py"
    s = summarize("read_file", {"path": "app.py", "offset": 1})
    assert s.target == "app.py"


def test_read_partial_shows_line_range():
    s = summarize("read_file", {"path": "app.py", "offset": 201, "limit": 200})
    assert s.target == "app.py:201-400"
    # First-N-lines read is still partial.
    s = summarize("read_file", {"path": "app.py", "limit": 200})
    assert s.target == "app.py:1-200"


def test_read_open_ended_shows_start_plus():
    # An offset with no limit pages to the cap; the end isn't known here.
    s = summarize("read_file", {"path": "app.py", "offset": 201})
    assert s.target == "app.py:201+"


def test_read_range_appends_after_clip_so_it_survives_long_paths():
    long_path = "src/marim_harness/" + "a" * 120 + ".py"
    s = summarize("read_file", {"path": long_path, "offset": 50, "limit": 10})
    assert s.target.endswith(":50-59"), s.target


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


def test_bash_prefers_description_as_headline():
    # The human-written description headlines the row; the command itself stays
    # visible in the expanded body (arg_lines) and the approval modal.
    s = summarize("bash", {
        "command": "uv run pytest --no-cov -q 2>&1 | tail -1",
        "description": "Run the test suite",
    })
    assert s.label == "Bash"
    assert s.target == "Run the test suite"


def test_bash_without_description_falls_back_to_command():
    s = summarize("bash", {"command": "ls -la"})
    assert s.target == "ls -la"


def test_bash_blank_description_falls_back_to_command():
    s = summarize("bash", {"command": "ls -la", "description": "   "})
    assert s.target == "ls -la"


def test_bash_description_head_clips_as_prose_not_middle():
    desc = "Count the total number of source lines across the whole project tree"
    s = summarize("bash", {"command": "wc -l", "description": desc}, cap=30)
    # Prose clips from the END (head kept), unlike a command's middle-clip.
    assert s.target.startswith("Count the total")
    assert s.target.endswith("…")
    assert len(s.target) <= 30


def test_bash_description_with_background_keeps_bg_badge():
    s = summarize("bash", {
        "command": "npm run dev", "description": "Start the dev server",
        "background": True,
    })
    assert s.target == "Start the dev server"
    assert s.badges == ("bg",)


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


def test_forget_global_scope_badge():
    s = summarize("forget", {"name": "my-fact", "scope": "global"})
    assert s.label == "Forget"
    assert s.target == "my-fact"
    assert s.badges == ("global",)
