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

The same boundary covers MCP: a granted MCP server is an open egress/mutation
channel (network, side effects) with no approval loop inside a spawn. The main
agent's own MCP calls are denied per-call in plan mode by the hook
``build_mcp_servers`` attaches (mcp/config.py ``make_approval_hook``), but a
spawn can't rely on that hook existing — ``HarnessBuilder.with_mcp_server``
accepts hookless servers — so plan mode must withhold the whole grant at
spawn/resume time, the same snapshot as the net-tool strip above.
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


# -- MCP grants (native spawns) ------------------------------------------------
# The MCP grant is resolved in SubagentRunner._prepare_spawn (the one funnel
# both spawn_agent and resume_spawn go through), so these tests probe that seam
# directly: what lands in prep.granted is exactly what the spawn's model run
# receives as toolsets.


def _live_fake_server(h, name: str = "mddocs", *, hook=None):
    """Install a stand-in MCP server on the harness's manager: carries the
    config name as ``id`` (server_name) and supports the ``.prefixed(name)``
    compose step granted_toolsets applies (returns itself as the marker).
    ``hook`` (when given) lands on ``process_tool_call`` exactly where
    ``build_mcp_servers`` puts the real approval hook, so the ask-mode
    prompting predicate reads it through the same seam; without it the server
    is hookless, like an embedder-supplied ``with_mcp_server`` toolset."""
    from types import SimpleNamespace

    srv = SimpleNamespace(id=name)
    srv.prefixed = lambda _name: srv
    if hook is not None:
        srv.process_tool_call = hook
    h.mcp._live_servers = h.mcp._live_servers + [srv]
    return srv


def _real_hook(name: str, trusted: bool):
    """The REAL approval hook config-built servers carry — built through
    ``make_approval_hook`` so these tests exercise the actual prompting-flag
    stamp, not a hand-faked attribute."""
    from marim_harness.mcp.config import make_approval_hook

    return make_approval_hook(name, trusted)


async def _prep(h, mcp_names):
    prep = await h.subagents._prepare_spawn(
        "explore", "look around", mcp_names, None, None, None, None, "s1",
        debug=False, t0=0.0,
    )
    assert not isinstance(prep, str), prep
    return prep


@pytest.mark.anyio
async def test_plan_mode_spawn_gets_no_mcp_toolsets(tmp_path: Path):
    """A plan-mode spawn's MCP grant is withheld entirely — granted toolsets
    empty, and the requested name is NOT reported as unknown (it exists; it is
    withheld by mode, and the spawner is told so via the withheld note)."""
    deps = _make_deps(tmp_path, mode=Mode.plan)
    h = _make_harness(_text_model(), deps)
    _live_fake_server(h)
    prep = await _prep(h, ["mddocs"])
    assert prep.granted == []
    assert prep.unknown == []
    assert prep.mcp_withheld is True


@pytest.mark.anyio
async def test_plan_mode_spawn_without_mcp_request_sets_no_withheld_flag(
    tmp_path: Path,
):
    """No requested servers ⇒ nothing was withheld, so no note is owed."""
    deps = _make_deps(tmp_path, mode=Mode.plan)
    h = _make_harness(_text_model(), deps)
    _live_fake_server(h)
    prep = await _prep(h, None)
    assert prep.granted == [] and prep.mcp_withheld is False
    assert h.subagents._withheld_mcp_note(prep) == ""


@pytest.mark.anyio
@pytest.mark.parametrize("mode", [Mode.auto, Mode.ask])
async def test_non_plan_modes_keep_mcp_grant(tmp_path: Path, mode: Mode):
    """auto/ask spawns keep the grant: on those modes the main agent's own MCP
    calls resolve through the per-call approval hook (auto runs / ask prompts
    for untrusted), so an up-front grant is consistent with the session."""
    deps = _make_deps(tmp_path, mode=mode)
    h = _make_harness(_text_model(), deps)
    srv = _live_fake_server(h)
    prep = await _prep(h, ["mddocs"])
    assert prep.granted == [srv]
    assert prep.mcp_withheld is False


@pytest.mark.anyio
async def test_mcp_withholding_is_evaluated_at_spawn_and_resume_time(
    tmp_path: Path,
):
    """Mode is snapshot per spawn, exactly like the net-tool strip: a flip to
    plan withholds the NEXT spawn's grant, a flip back restores it. The
    ``resumed=True`` leg pins the resume path too — resume_spawn funnels
    through this same _prepare_spawn call with the sidecar's recorded mcp
    names, so a spawn interrupted in auto mode and resumed under plan mode
    must come back WITHOUT its grant (and vice versa)."""
    deps = _make_deps(tmp_path, mode=Mode.auto)
    h = _make_harness(_text_model(), deps)
    srv = _live_fake_server(h)
    prep = await _prep(h, ["mddocs"])
    assert prep.granted == [srv]
    deps.workspace.mode = Mode.plan
    prep = await h.subagents._prepare_spawn(
        "explore", "look around", ["mddocs"], None, None, None, None, "s1",
        debug=False, t0=0.0, resumed=True,
    )
    assert not isinstance(prep, str)
    assert prep.granted == [] and prep.mcp_withheld is True
    deps.workspace.mode = Mode.ask
    prep = await _prep(h, ["mddocs"])
    assert prep.granted == [srv]


@pytest.mark.anyio
async def test_plan_mode_spawn_stamps_mcp_withheld_in_sidecar_meta(tmp_path: Path):
    """The sidecar meta records that THIS run's MCP grant was withheld, so a
    resumed card/stats view can show it — purely informational: resume logic
    must never read this field to decide the grant, it re-evaluates mode live
    through ``_prepare_spawn``/``_spawn_mcp_grant`` on every resume, exactly
    like a fresh spawn (see ``_spawn_mcp_grant``'s docstring)."""
    deps = _make_deps(tmp_path, mode=Mode.plan)
    h = _make_harness(_text_model(), deps)
    _live_fake_server(h)
    prep = await _prep(h, ["mddocs"])
    assert prep.meta is not None
    assert prep.meta["mcp_withheld"] is True


@pytest.mark.anyio
async def test_non_plan_mode_spawn_sidecar_meta_has_no_mcp_withheld(tmp_path: Path):
    """Outside plan mode nothing was withheld, so the sidecar meta says so."""
    deps = _make_deps(tmp_path, mode=Mode.auto)
    h = _make_harness(_text_model(), deps)
    _live_fake_server(h)
    prep = await _prep(h, ["mddocs"])
    assert prep.meta is not None
    assert prep.meta["mcp_withheld"] is False


@pytest.mark.anyio
async def test_withheld_note_names_plan_mode(tmp_path: Path):
    """The spawner is told the grant was withheld (mirrors the CLI path's
    not-forwarded note) instead of the servers silently vanishing."""
    deps = _make_deps(tmp_path, mode=Mode.plan)
    h = _make_harness(_text_model(), deps)
    _live_fake_server(h)
    prep = await _prep(h, ["mddocs"])
    note = h.subagents._withheld_mcp_note(prep)
    assert "plan mode" in note and "withheld" in note


# -- ask mode: per-server withholding ------------------------------------------
# A spawn's reach is decided entirely up front — a sub-agent has no approval
# round, so a granted server whose calls would PROMPT per-call in ask mode
# (make_approval_hook, untrusted) would surface mid-run prompts through the
# main-loop UI mid-spawn. The strict resolution: ask mode withholds exactly the
# prompting servers from the grant; trusted/hookless (auto-approved) servers
# stay granted.


@pytest.mark.anyio
async def test_ask_mode_withholds_prompting_server(tmp_path: Path):
    """An untrusted config-built server prompts per-call in ask mode, so an
    ask-mode spawn must not be granted it — withheld like plan mode, and NOT
    reported as unknown (it exists; it is withheld by mode)."""
    deps = _make_deps(tmp_path, mode=Mode.ask)
    h = _make_harness(_text_model(), deps)
    _live_fake_server(h, hook=_real_hook("mddocs", trusted=False))
    prep = await _prep(h, ["mddocs"])
    assert prep.granted == []
    assert prep.unknown == []
    assert prep.mcp_withheld is True
    assert prep.meta is not None and prep.meta["mcp_withheld"] is True


@pytest.mark.anyio
async def test_ask_mode_keeps_trusted_server(tmp_path: Path):
    """A trusted server's calls run without prompting in ask mode, so its
    up-front grant raises no mid-run prompt — it stays granted."""
    deps = _make_deps(tmp_path, mode=Mode.ask)
    h = _make_harness(_text_model(), deps)
    srv = _live_fake_server(h, hook=_real_hook("mddocs", trusted=True))
    prep = await _prep(h, ["mddocs"])
    assert prep.granted == [srv]
    assert prep.mcp_withheld is False


@pytest.mark.anyio
async def test_ask_mode_filters_grant_per_server(tmp_path: Path):
    """Withholding is per-server, not all-or-nothing: a mixed request keeps the
    trusted server and drops only the prompting one, and the note names
    exactly the dropped server."""
    deps = _make_deps(tmp_path, mode=Mode.ask)
    h = _make_harness(_text_model(), deps)
    ok = _live_fake_server(h, "docs", hook=_real_hook("docs", trusted=True))
    _live_fake_server(h, "prompty", hook=_real_hook("prompty", trusted=False))
    prep = await _prep(h, ["docs", "prompty"])
    assert prep.granted == [ok]
    assert prep.mcp_withheld is True
    note = h.subagents._withheld_mcp_note(prep)
    assert "prompty" in note and "docs" not in note.replace("prompty", "")
    assert "approval" in note and "ask" in note


@pytest.mark.anyio
async def test_auto_mode_grants_prompting_server(tmp_path: Path):
    """auto mode auto-approves every MCP call, so even an untrusted server's
    grant raises no prompt — the full grant is unchanged."""
    deps = _make_deps(tmp_path, mode=Mode.auto)
    h = _make_harness(_text_model(), deps)
    srv = _live_fake_server(h, hook=_real_hook("mddocs", trusted=False))
    prep = await _prep(h, ["mddocs"])
    assert prep.granted == [srv]
    assert prep.mcp_withheld is False


@pytest.mark.anyio
async def test_ask_withholding_is_evaluated_at_spawn_and_resume_time(
    tmp_path: Path,
):
    """Same snapshot funnel as the plan withhold: resume re-evaluates the mode
    live through _prepare_spawn/_spawn_mcp_grant, so a spawn interrupted in
    auto mode and resumed under ask mode comes back WITHOUT its prompting
    server (and gets it back once the mode allows)."""
    deps = _make_deps(tmp_path, mode=Mode.auto)
    h = _make_harness(_text_model(), deps)
    srv = _live_fake_server(h, hook=_real_hook("mddocs", trusted=False))
    prep = await _prep(h, ["mddocs"])
    assert prep.granted == [srv]
    deps.workspace.mode = Mode.ask
    prep = await h.subagents._prepare_spawn(
        "explore", "look around", ["mddocs"], None, None, None, None, "s1",
        debug=False, t0=0.0, resumed=True,
    )
    assert not isinstance(prep, str)
    assert prep.granted == [] and prep.mcp_withheld is True
    deps.workspace.mode = Mode.auto
    prep = await h.subagents._prepare_spawn(
        "explore", "look around", ["mddocs"], None, None, None, None, "s1",
        debug=False, t0=0.0, resumed=True,
    )
    assert not isinstance(prep, str)
    assert prep.granted == [srv] and prep.mcp_withheld is False


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


async def _captured_cli_run_kwargs(
    tmp_path: Path, monkeypatch, mode: Mode,
    mcp_names: list[str] | None = None,
) -> tuple[dict, str]:
    """Run a claude-cli spawn with ClaudeCliRunner.run stubbed out; return the
    kwargs run_cli invoked it with (pre-mapping allowed_tools, plus the
    Claude-Code-named disallowed_tools hard-deny list) and the spawn's report."""
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
    out = await h.subagents.run(
        "cli-netter", "fetch the docs", stream_id="s1", mcp_names=mcp_names
    )
    assert "done" in out
    return captured, out


@pytest.mark.anyio
async def test_cli_spawn_in_plan_mode_strips_net_tools(tmp_path: Path, monkeypatch):
    kwargs, _ = await _captured_cli_run_kwargs(tmp_path, monkeypatch, Mode.plan)
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
    kwargs, _ = await _captured_cli_run_kwargs(tmp_path, monkeypatch, Mode.plan)
    assert set(kwargs["disallowed_tools"] or []) == {"WebFetch", "WebSearch"}


@pytest.mark.anyio
@pytest.mark.parametrize("mode", [Mode.ask, Mode.auto])
async def test_cli_spawn_in_non_plan_modes_keeps_net_tools(
    tmp_path: Path, monkeypatch, mode: Mode,
):
    kwargs, _ = await _captured_cli_run_kwargs(tmp_path, monkeypatch, mode)
    assert set(kwargs["allowed_tools"]) >= NET_TOOLS
    # Nothing is hard-denied outside plan mode.
    assert not kwargs.get("disallowed_tools")


@pytest.mark.anyio
async def test_cli_spawn_never_receives_marim_mcp_config(
    tmp_path: Path, monkeypatch,
):
    """Marim's MCP grants are NOT forwarded to claude-cli spawns in ANY mode
    (the CLI uses its own MCP config; execute() emits a note instead), so
    there is no marim-side MCP channel to withhold in plan mode: nothing
    MCP-shaped may reach ClaudeCliRunner.run, and the spawner must see the
    not-forwarded note. This pins the assumption the native-side plan
    withholding leaves the CLI path alone."""
    kwargs, out = await _captured_cli_run_kwargs(
        tmp_path, monkeypatch, Mode.plan, mcp_names=["mddocs"]
    )
    assert not any("mcp" in k.lower() for k in kwargs)
    # Every kwarg except the two free-text ones (prompt/system_prompt) is
    # structured (tool lists, model id, flags): sweep only those for an
    # MCP-shaped value. Excluding the free-text kwargs avoids a false
    # positive if a task or system prompt legitimately mentions "MCP" as
    # English text, while still pinning that no MCP config crosses into
    # ClaudeCliRunner.run.
    structured = {
        k: v for k, v in kwargs.items() if k not in {"prompt", "system_prompt"}
    }
    assert not any("mcp" in str(v).lower() for v in structured.values())
    assert "not forwarded" in out and "mddocs" in out


def test_cli_argv_carries_no_mcp_flags():
    """Belt over braces for the same assumption at the argv layer: the CLI
    argv builder has no MCP-config surface at all, so a spawn can't smuggle
    marim MCP servers to the external process."""
    from marim_harness.subagents.cli_backend import build_cli_argv

    argv = build_cli_argv(
        binary="/bin/claude", prompt="do the task", permission_mode="plan",
        system_prompt="role", allowed_tools=["Read"],
        disallowed_tools=["WebFetch", "WebSearch"], model="opus",
        resume_session_id="sid", safe_mode=True,
    )
    assert not any("mcp" in a.lower() for a in argv)
