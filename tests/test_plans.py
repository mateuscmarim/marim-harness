from pathlib import Path

from marim_harness.workspace.plans import (
    format_plan,
    plan_slug,
    plans_dir,
    write_plan,
)


def test_plan_slug_is_stable_per_session_and_summary():
    a = plan_slug("sess-123456789abc", "Refactor the auth layer")
    b = plan_slug("sess-123456789abc", "Refactor the auth layer\nmore detail")
    assert a == b  # only the first line of the summary feeds the slug
    assert a == "sess-1234567-refactor-the-auth-layer"


def test_format_plan_has_frontmatter_and_checklist():
    md = format_plan(
        "Do the thing.",
        ["first step", "second step"],
        created="2026-06-28T12:00:00",
        session_id="sess-1",
    )
    assert md.startswith("---\n")
    assert "session: sess-1" in md
    assert "created: 2026-06-28T12:00:00" in md
    assert "- [ ] first step" in md
    assert "- [ ] second step" in md


def test_format_plan_drops_blank_steps():
    md = format_plan("S", ["a", "  ", ""], created="t", session_id="s")
    assert md.count("- [ ] ") == 1


def test_write_plan_creates_file_under_plans_dir(tmp_path: Path):
    path = write_plan(
        tmp_path,
        session_id="sess-1",
        summary="Refactor X",
        steps=["step one"],
        created="2026-06-28T12:00:00",
    )
    assert path == plans_dir(tmp_path) / "sess-1-refactor-x.md"
    assert path.exists()
    assert "step one" in path.read_text()
