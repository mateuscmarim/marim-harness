"""Run the shipped reference verbatim through upstream's typed workflow catalog."""

import asyncio
import re
from pathlib import Path

import pytest
from jsonschema import validate
from pydantic_ai import Agent, RunContext, StructuredDict
from pydantic_ai.agent import WrapperAgent
from pydantic_ai.exceptions import ModelRetry
from pydantic_ai.models.test import TestModel
from pydantic_ai.run import AgentRunResult
from pydantic_ai.usage import RunUsage
from pydantic_ai_harness import DynamicWorkflow

from marim_harness.config import builtin_root
from marim_harness.workflows.catalog import FINDINGS_SCHEMA, VERDICT_SCHEMA

SKILL = Path(builtin_root()) / "skills" / "deep-research" / "SKILL.md"


def _reference_script() -> str:
    text = SKILL.read_text(encoding="utf-8")
    blocks = re.findall(r"```python\n(.*?)```", text, flags=re.DOTALL)
    assert len(blocks) == 1, "SKILL.md must embed exactly one python reference script"
    return blocks[0]


def _finding(claim: str) -> dict:
    return {
        "claim": claim,
        "source": "https://example.org/" + claim,
        "quality": "high",
        "load_bearing": True,
    }


class _PipelineAgent(WrapperAgent):
    def __init__(self, name, schema, report):
        super().__init__(Agent(TestModel(), name=name, output_type=StructuredDict(schema)))
        self.schema = schema
        self.report = report
        self.tasks = []
        self.active = 0
        self.peak = 0

    async def run(self, user_prompt=None, **kwargs):
        self.tasks.append(user_prompt)
        self.active += 1
        self.peak = max(self.peak, self.active)
        try:
            await asyncio.sleep(0.01)
            report = self.report(user_prompt)
            validate(report, self.schema)
            return AgentRunResult(output=report)
        finally:
            self.active -= 1


def _research_report(task):
    if "healthy adults" in task:
        claims = ["holds"] if "first pass" in task else []
    elif "special populations" in task:
        claims = ["refuted"]
    else:
        claims = ["downgrade"]
    return {"findings": [_finding(claim) for claim in claims], "open_questions": []}


def _verification_report(task):
    verdict = next(v for v in ("holds", "refuted", "downgrade") if f"Claim: {v}" in task)
    return {"verdict": verdict, "reason": "checked source"}


async def _run_reference(research_report, verification_report):
    researcher = _PipelineAgent("research_findings", FINDINGS_SCHEMA, research_report)
    verifier = _PipelineAgent("verify_claim", VERDICT_SCHEMA, verification_report)
    capability = DynamicWorkflow(agents=[researcher, verifier], forward_usage=False)
    ctx = RunContext(deps=None, model=TestModel(), usage=RunUsage())
    toolset = await capability.get_toolset().for_run(ctx)
    async with toolset:
        tool = (await toolset.get_tools(ctx))["run_workflow"]
        result = await toolset.call_tool("run_workflow", {"code": _reference_script()}, ctx, tool)
    return result, researcher, verifier


@pytest.mark.anyio
async def test_reference_script_runs_coverage_and_adversarial_verification():
    result, researcher, verifier = await _run_reference(_research_report, _verification_report)
    bundle = result["result"]
    assert len(researcher.tasks) == 4  # three initial questions, one thin follow-up
    assert sum("first pass" in task for task in researcher.tasks) == 1
    assert researcher.peak == 3
    assert len(verifier.tasks) == 3
    assert verifier.peak == 3
    assert {f["claim"]: f["verified"] for f in bundle["findings"]} == {
        "holds": "holds: checked source",
        "downgrade": "downgrade: checked source",
    }
    assert bundle["dropped"] == [{"claim": "refuted", "reason": "checked source"}]
    assert bundle["open_questions"] == []
    assert "coverage round for 1" in result["output"]
    assert "verifying 3" in result["output"]


@pytest.mark.anyio
@pytest.mark.parametrize("failed_role", ["research", "verify"])
async def test_reference_script_failure_preserves_peer_previews_without_replay(failed_role):
    research_tasks = []
    verification_tasks = []

    def research(task):
        research_tasks.append(task)
        if failed_role == "research" and "healthy adults" in task:
            raise RuntimeError("research unavailable")
        return _research_report(task)

    def verify(task):
        verification_tasks.append(task)
        if "Claim: holds" in task:
            raise RuntimeError("verification unavailable")
        return _verification_report(task)

    # The selected upstream runtime propagates gather failures past helper-level
    # catches. Preserve its explicit error/preview contract instead of pretending
    # the script can recover every parallel child with a local try/except.
    with pytest.raises(ModelRetry, match="Completed sub-agent results") as error:
        await _run_reference(research, verify)
    assert "refuted" in str(error.value)  # completed peer report survives
    if failed_role == "research":
        assert len(research_tasks) == 3
        assert verification_tasks == []
    else:
        assert len(research_tasks) == 4
        assert len(verification_tasks) == 3


def test_skill_text_teaches_the_workflow_path():
    text = SKILL.read_text(encoding="utf-8")
    assert "run_workflow(code=...)" in text
    assert "MARIM_WORKFLOW_TIMEOUT" in text
    assert "spawn_agent" in text  # optional-extra fallback
    assert "last expression" in text.lower() or "returns data" in text.lower()
