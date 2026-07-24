"""Regression guard for the bundled ``examples/superpowers`` plugin (a trimmed
vendor of obra/superpowers): keep its manifest, SessionStart hook, and skills
parseable by marim's own loaders so the vendored copy can't silently rot."""

from pathlib import Path

from marim_harness.plugins.manifest import load_manifest
from marim_harness.workspace.skills import _parse_skill

PLUGIN_ROOT = Path(__file__).resolve().parents[1] / "examples" / "superpowers"

EXPECTED_SKILLS = {
    "brainstorming",
    "dispatching-parallel-agents",
    "executing-plans",
    "finishing-a-development-branch",
    "receiving-code-review",
    "requesting-code-review",
    "subagent-driven-development",
    "systematic-debugging",
    "test-driven-development",
    "using-git-worktrees",
    "using-superpowers",
    "verification-before-completion",
    "writing-plans",
    "writing-skills",
}


def test_manifest_loads():
    m = load_manifest(PLUGIN_ROOT)
    assert m.name == "superpowers"
    assert m.version == "6.2.0"


def test_session_start_hook_uses_marim_native_script():
    # The marim adaptation swaps upstream's Claude Code hook for a marim-native
    # one resolved via ${MARIM_PLUGIN_ROOT} (the plugin's own install dir).
    m = load_manifest(PLUGIN_ROOT)
    hooks = m.hooks_source()
    assert isinstance(hooks, dict)
    cmd = hooks["SessionStart"][0]["hooks"][0]["command"]
    assert "${MARIM_PLUGIN_ROOT}" in cmd
    assert "marim-session-start.sh" in cmd
    assert (PLUGIN_ROOT / "hooks" / "marim-session-start.sh").exists()


def test_all_skills_present_and_parse():
    dirs = {p.parent.name for p in (PLUGIN_ROOT / "skills").glob("*/SKILL.md")}
    assert dirs == EXPECTED_SKILLS
    # The hook injects using-superpowers on every session, so it must parse and
    # carry a description marim can surface.
    skill = _parse_skill(
        "plugin:superpowers", PLUGIN_ROOT / "skills" / "using-superpowers", plugin="superpowers"
    )
    assert skill is not None
    assert skill.qualified_name == "superpowers:using-superpowers"
    assert skill.description
