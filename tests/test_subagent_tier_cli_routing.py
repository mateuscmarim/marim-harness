"""Claude CLI tier routing: a ``backend: native`` role (built-in or custom)
whose resolved tier/model target is the reserved ``claude-cli:<model>`` value
transparently runs through the same Claude CLI orchestrator an explicit
``backend: claude-cli`` spec uses — see AD-007 and
``.specs/features/claude-cli-tier-routing/plan.md``.
"""

from pathlib import Path

import pytest
from pydantic_ai.messages import ModelResponse, TextPart
from pydantic_ai.models.function import FunctionModel

from marim_harness.config.model import SubagentTiers
from tests.conftest import _make_deps, _make_harness
from tests.fakes import fake_claude_bin, read_claude_argv


def _fake_cli(tmp_path: Path, text: str = "Done: report body") -> str:
    return fake_claude_bin(tmp_path, {"turns": [[{"text": "hi"}, {"text": text}]]})


def _dummy_model() -> FunctionModel:
    async def fn(messages, info):
        return ModelResponse(parts=[TextPart(content="unused")])

    return FunctionModel(fn)


def _write_native_agent(tmp_path: Path, name: str, *, tier: str | None = None) -> None:
    d = tmp_path / ".marim" / "agents"
    d.mkdir(parents=True, exist_ok=True)
    tier_line = f"tier: {tier}\n" if tier else ""
    (d / f"{name}.md").write_text(
        f"---\ndescription: a native worker\ntools: read_file\n{tier_line}---\nYou are a worker.\n",
        encoding="utf-8",
    )


def _write_codex_cli_agent(tmp_path: Path) -> None:
    d = tmp_path / ".marim" / "agents"
    d.mkdir(parents=True, exist_ok=True)
    (d / "codex-worker.md").write_text(
        "---\ndescription: codex worker\nbackend: codex-cli\ntools: read_file\n---\n"
        "You are a codex worker.\n",
        encoding="utf-8",
    )


@pytest.mark.anyio
async def test_readonly_native_role_routes_through_claude_cli_via_cheap_tier(tmp_path, monkeypatch):
    """A read-only native role (explore) with no CLI-specific agent type, tier
    override, or spec tier still ends up on Claude CLI once `cheap` names a
    claude-cli target — the tool-reach default (cheap) resolves it (AC1, AC2, AC5)."""
    monkeypatch.setenv("MARIM_CLAUDE_CLI_BIN", _fake_cli(tmp_path))
    tiers = SubagentTiers(cheap="claude-cli:haiku")
    deps = _make_deps(tmp_path)
    h = _make_harness(_dummy_model(), deps, subagent_tiers=tiers)
    report = await h.subagents.run("explore", "investigate the thing", stream_id="s1")
    assert "Done: report body" in report
    argv = read_claude_argv(tmp_path)
    assert "--model" in argv and argv[argv.index("--model") + 1] == "haiku"


@pytest.mark.anyio
async def test_mutating_native_role_routes_through_claude_cli_via_high_tier(tmp_path, monkeypatch):
    """A workspace-mutating native role (general) resolves to `high` by tool
    reach and runs through Claude CLI when `high` names a claude-cli target
    (AC1, AC3)."""
    monkeypatch.setenv("MARIM_CLAUDE_CLI_BIN", _fake_cli(tmp_path))
    tiers = SubagentTiers(high="claude-cli:sonnet")
    deps = _make_deps(tmp_path)
    h = _make_harness(_dummy_model(), deps, subagent_tiers=tiers)
    report = await h.subagents.run("general", "do the thing", stream_id="s1")
    assert "Done: report body" in report
    argv = read_claude_argv(tmp_path)
    assert "--model" in argv and argv[argv.index("--model") + 1] == "sonnet"


@pytest.mark.anyio
async def test_spec_tier_overrides_tool_reach_default_for_claude_routing(tmp_path, monkeypatch):
    """A custom native agent's own `tier:` frontmatter picks the target, ahead
    of the read-only/mutating tool-reach default (AC4)."""
    monkeypatch.setenv("MARIM_CLAUDE_CLI_BIN", _fake_cli(tmp_path))
    _write_native_agent(tmp_path, "picky", tier="high")
    tiers = SubagentTiers(cheap="claude-cli:haiku", high="claude-cli:opus")
    deps = _make_deps(tmp_path)
    h = _make_harness(_dummy_model(), deps, subagent_tiers=tiers)
    await h.subagents.run("picky", "task", stream_id="s1")
    argv = read_claude_argv(tmp_path)
    assert argv[argv.index("--model") + 1] == "opus"  # spec tier (high) wins, not cheap


@pytest.mark.anyio
async def test_explicit_claude_cli_model_override_routes_native_role_to_cli(tmp_path, monkeypatch):
    """An allowed explicit `model="claude-cli:<model>"` override on a spawn
    call selects the same Claude execution target a tier value would (AC6)."""
    monkeypatch.setenv("MARIM_CLAUDE_CLI_BIN", _fake_cli(tmp_path))
    tiers = SubagentTiers(cheap="claude-cli:opus")  # makes "claude-cli:opus" the allowlist
    deps = _make_deps(tmp_path)
    h = _make_harness(_dummy_model(), deps, subagent_tiers=tiers)
    await h.subagents.run("explore", "task", stream_id="s1", model="claude-cli:opus")
    argv = read_claude_argv(tmp_path)
    assert argv[argv.index("--model") + 1] == "opus"


@pytest.mark.anyio
async def test_disallowed_claude_cli_override_falls_back_to_tier_default(tmp_path, monkeypatch):
    """A `model=` override naming a claude-cli target OUTSIDE the configured
    allowlist is discarded in favor of the resolved tier target — including
    when that tier target is itself Claude CLI (AC10)."""
    monkeypatch.setenv("MARIM_CLAUDE_CLI_BIN", _fake_cli(tmp_path))
    tiers = SubagentTiers(cheap="claude-cli:haiku")
    deps = _make_deps(tmp_path)
    h = _make_harness(_dummy_model(), deps, subagent_tiers=tiers)
    await h.subagents.run("explore", "task", stream_id="s1", model="claude-cli:not-allowed")
    argv = read_claude_argv(tmp_path)
    assert argv[argv.index("--model") + 1] == "haiku"  # dropped override → tier default


@pytest.mark.anyio
async def test_codex_cli_backend_unaffected_by_claude_tier_config(tmp_path):
    """An explicit `backend: codex-cli` spec keeps its existing (removed)
    dispatch regardless of Claude tier configuration — Claude tier routing
    applies only to `backend: native` (AC8, AC9)."""
    _write_codex_cli_agent(tmp_path)
    tiers = SubagentTiers(cheap="claude-cli:haiku", high="claude-cli:opus")
    deps = _make_deps(tmp_path)
    h = _make_harness(_dummy_model(), deps, subagent_tiers=tiers)
    out = await h.subagents.run("codex-worker", "task", stream_id="s1")
    assert "removed" in out.lower() or "codex" in out.lower()


@pytest.mark.anyio
async def test_tier_routed_claude_schema_uses_prompt_contract(tmp_path, monkeypatch):
    """A tier-routed Claude spawn enforces a declared output schema through the
    existing Claude prompt-contract path, not native structured output — same
    as an explicit `backend: claude-cli` spawn (AC14)."""
    monkeypatch.setenv("MARIM_CLAUDE_CLI_BIN", _fake_cli(tmp_path))
    tiers = SubagentTiers(cheap="claude-cli:haiku")
    deps = _make_deps(tmp_path)
    h = _make_harness(_dummy_model(), deps, subagent_tiers=tiers)
    seen = {}

    async def fake_execute(defn, task, *args, **kwargs):
        seen["task"] = task
        return "ok"

    h.subagents._cli.execute = fake_execute
    out = await h.subagents.run(
        "explore",
        "task",
        "s1",
        output_schema={"type": "object", "properties": {"ok": {"type": "boolean"}}},
    )
    assert out == "ok"
    assert "Output contract" in seen["task"]


@pytest.mark.anyio
async def test_unconfigured_tiers_native_role_stays_native(tmp_path):
    """With no claude-cli tier target configured, a native role never touches
    the CLI orchestrator (regression guard for the new branch)."""

    async def fn(messages, info):
        return ModelResponse(parts=[TextPart(content="plain native report")])

    deps = _make_deps(tmp_path)
    h = _make_harness(FunctionModel(fn), deps)
    out = await h.subagents.run("explore", "task", stream_id="s1")
    assert out == "plain native report"
