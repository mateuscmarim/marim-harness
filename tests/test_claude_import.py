from pathlib import Path

from marim_harness.workspace import claude_import


def test_project_slug_replaces_slashes_and_dots():
    """Claude names its project dirs by replacing every `/` and `.` with `-`.
    The leading `-` falls out of the leading `/`. Paths here are absolute and
    non-existent, so the internal resolve() is an identity transform."""
    slug = claude_import.claude_project_slug("/home/x/Projects/marim.dev/marim-harness")
    assert slug == "-home-x-Projects-marim-dev-marim-harness"


def test_project_slug_doubles_dash_for_dot_directories():
    """`/.local` contributes both the separator dash and the dot dash, which is
    why real Claude dirs read `-home-x--local-share-...`."""
    slug = claude_import.claude_project_slug("/home/x/.local/share/fcstudio")
    assert slug == "-home-x--local-share-fcstudio"


def test_project_slug_normalizes_trailing_slash_and_dot_segments():
    assert claude_import.claude_project_slug("/home/x/proj/") == "-home-x-proj"
    assert claude_import.claude_project_slug("/home/x/a/../proj") == "-home-x-proj"


def test_config_dir_honors_env_override(tmp_path: Path):
    got = claude_import.claude_config_dir({"CLAUDE_CONFIG_DIR": str(tmp_path / "cc")})
    assert got == tmp_path / "cc"


def test_config_dir_ignores_blank_override_and_falls_back_to_home():
    got = claude_import.claude_config_dir({"CLAUDE_CONFIG_DIR": "   "})
    assert got == Path.home() / ".claude"


def test_config_dir_defaults_to_dot_claude_in_home():
    assert claude_import.claude_config_dir({}) == Path.home() / ".claude"


def test_memory_dir_composes_projects_slug_memory(tmp_path: Path):
    got = claude_import.claude_memory_dir(
        "/home/x/Projects/app", config_dir=tmp_path / "cc"
    )
    assert got == tmp_path / "cc" / "projects" / "-home-x-Projects-app" / "memory"
