from pathlib import Path

from pydantic_ai.models.test import TestModel

from marim_harness.runtime.harness import Harness, HarnessConfig
from marim_harness.tools.provider import BuiltinToolProvider, ToolGroups
from tests.conftest import _make_deps  # same helper test_provider uses


def _harness(tmp_path: Path, **cfg_kwargs) -> Harness:
    return Harness(
        TestModel(),
        BuiltinToolProvider(),
        _make_deps(tmp_path),
        "instructions",
        config=HarnessConfig(lsp_enabled=False, **cfg_kwargs),
    )


def _instruction_closure(agent, name):
    """Return the callable behind either supported pydantic-ai registration shape."""
    return next(
        (
            fn
            for registered in agent._instructions  # noqa: SLF001
            if callable(fn := getattr(registered, "instruction", registered))
            and getattr(fn, "__name__", None) == name
        ),
        None,
    )


def test_global_instructions_gate(tmp_path, monkeypatch):
    """global_instructions gates whether the user-level instructions file is
    ever read: True registers and invokes the closure that reads it; False
    never even registers it. It also gates ``_plugin_instructions`` — that
    closure reads the embedding user's installed-plugin state too (see
    register_instructions' docstring), so it shares the same gate rather than
    registering unconditionally."""
    import marim_harness.runtime.instructions as instr

    calls = []
    monkeypatch.setattr(instr, "load_global_instructions", lambda: calls.append(1) or "global text")

    h_on = _harness(tmp_path, global_instructions=True)
    h_off = _harness(tmp_path, global_instructions=False)

    # There is no public accessor for registered instruction functions. Pydantic
    # AI 2.43 wraps them in SourcedInstruction; older supported releases stored
    # the callables directly.
    on_closure = _instruction_closure(h_on.agent, "_global_instructions")
    off_closure = _instruction_closure(h_off.agent, "_global_instructions")
    assert on_closure is not None, "global_instructions=True must register the closure"
    assert off_closure is None, "global_instructions=False must not register the closure"

    plugin_on = _instruction_closure(h_on.agent, "_plugin_instructions")
    plugin_off = _instruction_closure(h_off.agent, "_plugin_instructions")
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

    h_off = _harness(tmp_path, groups=ToolGroups(files_write=False))
    h_on = _harness(tmp_path, groups=None)

    off_closure = _instruction_closure(h_off.agent, "_scratchpad")
    on_closure = _instruction_closure(h_on.agent, "_scratchpad")
    assert off_closure is None, "files_write=False must not register _scratchpad"
    assert on_closure is not None, "groups=None must still register _scratchpad"
