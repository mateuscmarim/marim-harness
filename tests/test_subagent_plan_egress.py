"""Plan mode must close outbound network egress for sub-agent spawns.

Plan mode denies fetch_url/web_search on the MAIN agent (runtime/permissions.py
`_plan_decision`): the mode is presented as local-research-only, and a
prompt-injected agent could otherwise read any host file and exfiltrate it
through a fetch URL or search query with zero approval. But sub-agents register
their tools PLAIN — no approval round, no plan denial — so a spawn's reach is
decided entirely up front by the granted tool-name set. These tests pin the
boundary at that decision point: a spawn created while the session is in plan
mode must not be granted web_search/fetch_url on any spawn path (native or
claude-cli), while auto/ask modes keep the definition's own grant.
"""

from pathlib import Path

import pytest

from marim_harness.runtime.permissions import Mode
from marim_harness.tools.names import NET_TOOLS
from tests.conftest import _make_deps, _make_harness, _text_model


def _tool_names(sub) -> set[str]:
    return set(sub._function_toolset.tools.keys())


def test_plan_mode_spawn_has_no_net_tools(tmp_path: Path):
    """A spawn built in plan mode is stripped of web_search/fetch_url — the
    explore built-in grants NET_TOOLS, and without this strip the ungated
    spawn_agent tool would hand the model unapproved network egress."""
    deps = _make_deps(tmp_path, mode=Mode.plan)
    h = _make_harness(_text_model(), deps)
    sub, err = h.subagents.build("explore")
    assert err is None and sub is not None
    names = _tool_names(sub)
    assert not (names & NET_TOOLS)
    # Local reads survive — plan mode is local-research-only, not tool-less.
    assert "read_file" in names and "grep" in names


@pytest.mark.parametrize("mode", [Mode.auto, Mode.ask])
def test_non_plan_modes_keep_net_tools(tmp_path: Path, mode: Mode):
    """auto/ask spawns keep the definition's network grant: on those modes the
    main agent's own net tools resolve through approval (auto-approved / user
    prompted), so a spawn's up-front grant is consistent with the session."""
    deps = _make_deps(tmp_path, mode=mode)
    h = _make_harness(_text_model(), deps)
    sub, err = h.subagents.build("explore")
    assert err is None and sub is not None
    assert _tool_names(sub) >= NET_TOOLS


def test_net_strip_is_evaluated_at_spawn_time(tmp_path: Path):
    """Mode is dynamic per session (WorkspaceConfig.mode is rewritten in place
    by set_mode/cycle_mode); each spawn snapshots the mode at build time, the
    same way allow_gated already does — a flip to plan strips the next spawn,
    a flip back restores it."""
    deps = _make_deps(tmp_path, mode=Mode.auto)
    h = _make_harness(_text_model(), deps)
    sub, _ = h.subagents.build("explore")
    assert _tool_names(sub) >= NET_TOOLS
    deps.workspace.mode = Mode.plan
    sub, _ = h.subagents.build("explore")
    assert not (_tool_names(sub) & NET_TOOLS)
    deps.workspace.mode = Mode.ask
    sub, _ = h.subagents.build("explore")
    assert _tool_names(sub) >= NET_TOOLS


# -- claude-cli backend --------------------------------------------------------
# The CLI degrades any non-auto mode to `--permission-mode plan`, and Claude
# Code's own plan mode AUTO-ALLOWS its web research tools (WebSearch/WebFetch).
# --allowedTools is additive pre-approval only, so stripping the net tools from
# the allowlist (or omitting the flag entirely when the stripped set maps
# empty) denies nothing. The hard deny headless -p honors is --disallowedTools:
# a plan-mode spawn must both keep the net tools out of --allowedTools AND name
# their Claude Code counterparts in --disallowedTools; auto/ask spawns must not
# be denied anything.


def _write_cli_net_agent(ws: Path) -> None:
    d = ws / ".marim" / "agents"
    d.mkdir(parents=True, exist_ok=True)
    (d / "cli-netter.md").write_text(
        "---\ndescription: CLI worker with net.\nbackend: claude-cli\n"
        "tools: read_file, web_search, fetch_url\n---\nYou fetch things.\n",
        encoding="utf-8",
    )


async def _captured_cli_run_kwargs(tmp_path: Path, monkeypatch, mode: Mode) -> dict:
    """Run a claude-cli spawn with ClaudeCliRunner.run stubbed out; return the
    kwargs run_cli invoked it with (pre-mapping allowed_tools, plus the
    Claude-Code-named disallowed_tools hard-deny list)."""
    from pydantic_ai.usage import RunUsage

    from marim_harness.subagents import cli_backend

    # The cli-netter definition lives in the project-local `.marim/agents`,
    # which loads only in a trusted workspace (same gate as hooks/MCP). Trust
    # is safe to flip per-test: the discovery cache fingerprints the root list,
    # so trusted/untrusted callers never share an entry.
    monkeypatch.setenv("MARIM_TRUST_PROJECT_HOOKS", "1")
    monkeypatch.setattr(cli_backend, "resolve_cli_binary", lambda: "/bin/fake-claude")
    captured: dict = {}

    async def fake_run(self, **kwargs):
        captured.update(kwargs)
        return cli_backend.CliResult(output="done", usage=RunUsage())

    monkeypatch.setattr(cli_backend.ClaudeCliRunner, "run", fake_run)
    _write_cli_net_agent(tmp_path)
    deps = _make_deps(tmp_path, mode=mode)
    h = _make_harness(_text_model(), deps)
    out = await h.subagents.run("cli-netter", "fetch the docs", stream_id="s1")
    assert "done" in out
    return captured


@pytest.mark.anyio
async def test_cli_spawn_in_plan_mode_strips_net_tools(tmp_path: Path, monkeypatch):
    kwargs = await _captured_cli_run_kwargs(tmp_path, monkeypatch, Mode.plan)
    tools = set(kwargs["allowed_tools"])
    assert not (tools & NET_TOOLS)
    assert "read_file" in tools


@pytest.mark.anyio
async def test_cli_spawn_in_plan_mode_hard_denies_cc_web_tools(
    tmp_path: Path, monkeypatch,
):
    """Allowlist absence is not a denial under CC's plan permission mode (it
    auto-allows web research tools), so the plan-mode spawn must ALSO pass the
    web tools' Claude Code names as a --disallowedTools hard deny — exactly
    those two, nothing else."""
    kwargs = await _captured_cli_run_kwargs(tmp_path, monkeypatch, Mode.plan)
    assert set(kwargs["disallowed_tools"] or []) == {"WebFetch", "WebSearch"}


@pytest.mark.anyio
@pytest.mark.parametrize("mode", [Mode.ask, Mode.auto])
async def test_cli_spawn_in_non_plan_modes_keeps_net_tools(
    tmp_path: Path, monkeypatch, mode: Mode,
):
    kwargs = await _captured_cli_run_kwargs(tmp_path, monkeypatch, mode)
    assert set(kwargs["allowed_tools"]) >= NET_TOOLS
    # Nothing is hard-denied outside plan mode.
    assert not kwargs.get("disallowed_tools")
