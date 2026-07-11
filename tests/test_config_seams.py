from pathlib import Path

import pytest
from pydantic_ai.models.test import TestModel

from marim_harness.forge.models import CiStatus, PullRequest
from marim_harness.runtime.harness import Harness, HarnessConfig
from marim_harness.tools.provider import BuiltinToolProvider, ToolGroups
from tests.conftest import _make_deps  # same helper test_provider uses


def _harness(tmp_path: Path, **cfg_kwargs) -> Harness:
    return Harness(
        TestModel(), BuiltinToolProvider(), _make_deps(tmp_path), "instructions",
        config=HarnessConfig(lsp_enabled=False, **cfg_kwargs),
    )


class _FakeForgeBackend:
    """Minimal ForgeBackend stub (see forge/backend.py) — only attachment is
    asserted here, so every method just raises."""

    async def list_prs(self, state: str, limit: int) -> list[PullRequest]:
        raise NotImplementedError

    async def view_pr(self, number: int | None, branch: str | None) -> PullRequest | None:
        raise NotImplementedError

    async def ci_status(self, branch: str) -> CiStatus:
        raise NotImplementedError

    async def create_pr(
        self, title: str, body: str, base: str | None, draft: bool, head: str
    ) -> PullRequest:
        raise NotImplementedError

    async def checkout_pr(self, number: int, create_branch: bool) -> str:
        raise NotImplementedError


@pytest.fixture
def fake_forge_backend():
    return _FakeForgeBackend()


@pytest.mark.anyio
async def test_explicit_forge_backend_attaches_toolset(tmp_path, fake_forge_backend, monkeypatch):
    """An explicit backend attaches forge tools directly, bypassing
    select_backend's tea-on-PATH auto-detection entirely — and the specific
    fake instance passed in is the one actually bound, not a same-named
    toolset that happened to come from real tea auto-detection (which would
    also satisfy a bare "list_prs" in toolset_tools" check on a machine where
    tea is on PATH with a configured login)."""
    import marim_harness.tools.forge_tools as forge_tools_mod

    def _must_not_run(*a, **k):
        raise AssertionError(
            "select_backend (tea auto-detect) ran despite an explicit forge_backend"
        )

    monkeypatch.setattr(forge_tools_mod, "select_backend", _must_not_run)

    h = _harness(tmp_path, forge_enabled=True, forge_backend=fake_forge_backend)
    toolsets_with_list_prs = [
        ts for ts in h.agent.toolsets if "list_prs" in getattr(ts, "tools", {})
    ]
    assert len(toolsets_with_list_prs) == 1
    list_prs_tool = toolsets_with_list_prs[0].tools["list_prs"]

    # A real backend (tea auto-detect or otherwise) would either succeed or
    # raise ForgeError, which list_prs catches and turns into a "Forge
    # error: ..." string. Our fake raises a bare NotImplementedError, which
    # list_prs does NOT catch — seeing it propagate here proves the *specific*
    # fake_forge_backend instance we passed in is the one actually bound.
    with pytest.raises(NotImplementedError):
        await list_prs_tool.function(None, "open", 30)


def test_explicit_forge_backend_disabled_flag_skips_toolset(tmp_path, fake_forge_backend):
    """forge_enabled=False must still gate attachment even with an explicit
    backend given — the flag is a separate switch from backend selection."""
    h = _harness(tmp_path, forge_enabled=False, forge_backend=fake_forge_backend)
    toolset_tools = {n for ts in h.agent.toolsets for n in getattr(ts, "tools", {})}
    assert "list_prs" not in toolset_tools


def test_global_instructions_gate(tmp_path, monkeypatch):
    """global_instructions gates whether the user-level instructions file is
    ever read: True registers and invokes the closure that reads it; False
    never even registers it. It also gates ``_plugin_instructions`` — that
    closure reads the embedding user's installed-plugin state too (see
    register_instructions' docstring), so it shares the same gate rather than
    registering unconditionally."""
    import marim_harness.runtime.instructions as instr

    calls = []
    monkeypatch.setattr(
        instr, "load_global_instructions", lambda: calls.append(1) or "global text"
    )

    h_on = _harness(tmp_path, global_instructions=True)
    h_off = _harness(tmp_path, global_instructions=False)

    # pydantic-ai keeps registered @agent.instructions closures in the plain
    # list Agent._instructions, unwrapped (there is no public accessor);
    # verified against the installed version with `uv run python -c
    # "from pydantic_ai import Agent, RunContext
    # a=Agent('test')
    # @a.instructions
    # def foo(ctx: RunContext): return 'x'
    # print(a._instructions[0] is foo)"` -> True.
    def _closure(agent, name):
        return next(
            (
                fn for fn in agent._instructions
                if callable(fn) and getattr(fn, "__name__", None) == name
            ),
            None,
        )

    on_closure = _closure(h_on.agent, "_global_instructions")
    off_closure = _closure(h_off.agent, "_global_instructions")
    assert on_closure is not None, "global_instructions=True must register the closure"
    assert off_closure is None, "global_instructions=False must not register the closure"

    plugin_on = _closure(h_on.agent, "_plugin_instructions")
    plugin_off = _closure(h_off.agent, "_plugin_instructions")
    assert plugin_on is not None, "global_instructions=True must register _plugin_instructions"
    assert plugin_off is None, "global_instructions=False must not register _plugin_instructions"

    # Two closures share this gate, so off drops exactly two vs. on.
    assert len(h_off.agent._instructions) == len(h_on.agent._instructions) - 2

    # Behavioral: actually evaluate the registered closure (it never touches
    # ctx, so a plain None stands in for RunContext) and confirm it reaches
    # load_global_instructions — proving True really does read the file, not
    # just that a same-named function object exists.
    result = on_closure(None)
    assert calls == [1]
    assert "global text" in result

    # And confirm the gate-off harness truly never invokes it: nothing else
    # in this test called load_global_instructions, so calls is untouched.
    assert calls == [1]


def test_scratchpad_instructions_gate_on_files_write_group(tmp_path):
    """The _scratchpad closure advertises "write_file/edit_file writes there
    do not need approval" — a claim that only holds when those tools are
    actually registered. groups=ToolGroups(files_write=False) (no write_file/
    edit_file on the agent) must drop the closure entirely; groups=None (the
    HarnessConfig default, "every group is on") must keep registering it,
    same as every other gated closure in register_instructions."""

    def _closure(agent, name):
        return next(
            (
                fn for fn in agent._instructions  # noqa: SLF001
                if callable(fn) and getattr(fn, "__name__", None) == name
            ),
            None,
        )

    h_off = _harness(tmp_path, groups=ToolGroups(files_write=False))
    h_on = _harness(tmp_path, groups=None)

    off_closure = _closure(h_off.agent, "_scratchpad")
    on_closure = _closure(h_on.agent, "_scratchpad")
    assert off_closure is None, "files_write=False must not register _scratchpad"
    assert on_closure is not None, "groups=None must still register _scratchpad"
