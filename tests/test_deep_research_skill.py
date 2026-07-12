"""The deep-research SKILL.md embeds a reference workflow script. Skill text
with a script the sandbox rejects is a shipped bug, so this test extracts the
fenced block and puts it through the same parse + static-validation gates the
engine applies to a live script."""

import re
from pathlib import Path

from pydantic_monty import Monty

from marim_harness.config import builtin_root
from marim_harness.workflows.engine import _VALIDATION_PREFIX

SKILL = Path(builtin_root()) / "skills" / "deep-research" / "SKILL.md"


def _reference_script() -> str:
    text = SKILL.read_text(encoding="utf-8")
    blocks = re.findall(r"```python\n(.*?)```", text, flags=re.DOTALL)
    assert len(blocks) == 1, "SKILL.md must embed exactly one python reference script"
    return blocks[0]


def test_reference_script_parses_and_validates_as_monty():
    script = _reference_script()
    monty = Monty(script, inputs=["args"], script_name="workflow.py")
    monty.type_check(_VALIDATION_PREFIX)  # raises MontyTypingError on failure


def test_skill_text_teaches_the_workflow_path():
    text = SKILL.read_text(encoding="utf-8")
    assert "run_workflow" in text
    assert "timeout_secs" in text
    # The fallback for installs without the [workflows] extra must survive edits.
    assert "spawn_agent" in text
    # Synthesis stays in the main turn; the script returns data.
    assert "last expression" in text.lower() or "returns data" in text.lower()
