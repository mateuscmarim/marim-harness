"""End-to-end plan-mode test driven through the real turn engine.

A scripted ``FunctionModel`` stands in for the LLM so the test is deterministic
and needs no provider, but everything else is the real runtime: prompt
assembly, the approval-round loop, ``resolve_approvals``'s plan-mode policy, the
``present_plan`` tool, the plan-artifact writer, and the in-tool mode flip.

The scripted turn mimics how a real planning turn unfolds:
  1. In plan mode the model first tries a mutating ``edit_file`` (must be denied)
     and a read-only ``bash`` (must be allowed).
  2. Seeing the results, it calls ``present_plan`` with a summary + steps.
  3. The user picks "Execute hands-off (auto)"; the mode flips and the model
     proceeds with a final answer.

We then assert the whole pipeline's observable effects.
"""

import pytest
from pydantic_ai.messages import (
    ModelResponse,
    TextPart,
    ToolCallPart,
)
from pydantic_ai.models.function import FunctionModel

from marim_harness.runtime.controller import TurnController
from marim_harness.runtime.harness import HarnessConfig, build_collaborators
from marim_harness.runtime.permissions import Mode
from marim_harness.tools.provider import BuiltinToolProvider
from tests.conftest import _make_deps


def _returned_tool_names(messages) -> set[str]:
    """Tool names that already have a result in the history (so the scripted
    model can tell which step of the turn it is on)."""
    names: set[str] = set()
    for m in messages:
        for part in getattr(m, "parts", []):
            name = getattr(part, "tool_name", None)
            if name and part.__class__.__name__ in {"ToolReturnPart", "RetryPromptPart"}:
                names.add(name)
    return names


def _scripted_model(messages, info) -> ModelResponse:
    returned = _returned_tool_names(messages)
    if "present_plan" in returned:
        # Step 3: plan approved, mode is now auto — give the final answer.
        return ModelResponse(parts=[TextPart(content="Plan approved — executing now.")])
    if "bash" in returned or "edit_file" in returned:
        # Step 2: research done, present the plan.
        return ModelResponse(
            parts=[
                ToolCallPart(
                    tool_name="present_plan",
                    args={
                        "summary": "Split the parser into smaller functions.",
                        "steps": ["Extract the tokenizer", "Add unit tests"],
                    },
                    tool_call_id="call_present",
                )
            ]
        )
    # Step 1: try a mutating edit (should be denied) and a read-only command
    # (should be allowed) while planning.
    return ModelResponse(
        parts=[
            ToolCallPart(
                tool_name="edit_file",
                args={"path": "parser.py", "edits": [{"old_string": "a", "new_string": "b"}]},
                tool_call_id="call_edit",
            ),
            ToolCallPart(
                tool_name="bash",
                args={"command": "ls"},
                tool_call_id="call_ls",
            ),
        ]
    )


def _build_tc(model, tmp_path, ask_user):
    deps = _make_deps(tmp_path, mode=Mode.plan, ask_user=ask_user)
    collabs = build_collaborators(
        model,
        BuiltinToolProvider(),
        deps,
        "You are a coding agent.",
        HarnessConfig(),
        get_model=lambda: model,
    )
    tc = TurnController(
        agent=collabs.agent,
        session=collabs.session,
        checkpoints=collabs.checkpoints,
        hooks=collabs.hooks,
        mcp=collabs.mcp,
        deps=deps,
        get_model=lambda: model,
    )
    return tc, deps


@pytest.mark.anyio
async def test_plan_mode_full_cycle(tmp_path):
    """Plan → deny mutation / allow read-only bash → present_plan → approve →
    mode flips, plan file written, checklist populated."""
    chosen = []

    async def fake_ask(questions):
        chosen.append(questions[0].question)
        return {questions[0].header: "Execute hands-off (auto)"}

    tc, deps = _build_tc(FunctionModel(_scripted_model), tmp_path, fake_ask)

    final = await tc.run_turn("Plan a refactor of the parser.")

    # 1) The turn completed with the model's post-approval answer.
    assert final == "Plan approved — executing now."

    # 2) The user was actually asked how to execute (present_plan reached the UI).
    assert chosen, "present_plan never prompted the user"

    # 3) The approval mode flipped in place from plan -> auto.
    assert deps.workspace.mode is Mode.auto

    # 4) The plan artifact was written to .marim/plans/.
    plan_files = list((tmp_path / ".marim" / "plans").glob("*.md"))
    assert len(plan_files) == 1
    body = plan_files[0].read_text()
    assert "Extract the tokenizer" in body
    assert "Add unit tests" in body

    # 5) The steps were mirrored into the task checklist.
    assert [t.text for t in deps.tasks.items] == ["Extract the tokenizer", "Add unit tests"]

    # 6) Plan-mode policy held during research: the mutating edit was denied
    #    while the read-only `ls` was allowed (its result is in the history).
    history_text = repr(tc.session.history)
    assert "read-only plan mode" in history_text  # edit_file denial reason
    returned = _returned_tool_names(tc.session.history)
    assert "bash" in returned  # the read-only command was approved + executed

    # 7) The planning posture was injected into the turn prompt.
    assert "PLAN MODE" in history_text
