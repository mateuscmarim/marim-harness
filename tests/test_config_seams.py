from pathlib import Path

from pydantic_ai.messages import ModelResponse, TextPart
from pydantic_ai.models.function import FunctionModel
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


def _instruction_text(harness):
    captured = []

    def observe(messages, info):
        captured.append(info.instructions)
        return ModelResponse(parts=[TextPart("done")])

    with harness.agent.override(model=FunctionModel(observe)):
        harness.agent.run_sync("Inspect instructions", deps=harness.deps)
    assert len(captured) == 1
    assert captured[0], "Must inspect an actual nonempty model prompt"
    return captured[0]


def test_global_instructions_gate(tmp_path, monkeypatch):
    """Disabled global/plugin readers are untouched; enabled content reaches the model."""
    import marim_harness.runtime.instructions as instr

    global_reads = []
    plugin_reads = []
    monkeypatch.setattr(
        instr, "load_global_instructions", lambda: global_reads.append(1) or "GLOBAL-MARKER"
    )
    monkeypatch.setattr(
        instr,
        "plugin_instruction_texts",
        lambda *args, **kwargs: plugin_reads.append(1) or [("fixture", "PLUGIN-MARKER")],
    )
    h_off = _harness(tmp_path, global_instructions=False)
    off_text = _instruction_text(h_off)
    assert global_reads == []
    assert plugin_reads == []
    assert "GLOBAL-MARKER" not in off_text
    assert "PLUGIN-MARKER" not in off_text

    h_on = _harness(tmp_path, global_instructions=True)
    on_text = _instruction_text(h_on)
    assert global_reads == [1]
    assert plugin_reads == [1]
    assert "GLOBAL-MARKER" in on_text
    assert "PLUGIN-MARKER" in on_text


def test_scratchpad_instructions_gate_on_files_write_group(tmp_path, monkeypatch):
    """Only a harness with write tools may evaluate scratchpad bypass instructions."""
    import marim_harness.runtime.instructions as instr

    reads = []
    render_scratchpad = instr._scratchpad_block

    def tracked_instruction(ctx):
        reads.append(1)
        return render_scratchpad(ctx)

    # The output-limit capability also uses get_scratchpad; track the instruction
    # source specifically so its independent storage lookup remains permitted.
    monkeypatch.setattr(instr, "_scratchpad_block", tracked_instruction)

    h_off = _harness(tmp_path, groups=ToolGroups(files_write=False))
    h_off.deps.services.get_scratchpad = lambda: tmp_path / "scratchpad-fixture"
    off_text = _instruction_text(h_off)
    assert reads == []
    assert "scratchpad-fixture" not in off_text
    assert "write_file/edit_file writes there" not in off_text

    h_on = _harness(tmp_path, groups=None)
    h_on.deps.services.get_scratchpad = lambda: tmp_path / "scratchpad-fixture"
    on_text = _instruction_text(h_on)
    assert reads == [1]
    assert str(tmp_path / "scratchpad-fixture") in on_text
    assert "write_file/edit_file writes there" in on_text
