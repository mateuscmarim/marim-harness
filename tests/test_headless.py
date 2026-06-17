import io
import json
from pathlib import Path

import pytest

from marim_harness.deps import Deps
from marim_harness.permissions import Mode


def _harness(tmp_path: Path, output_text: str = "hello from the model"):
    from pydantic_ai.models.test import TestModel

    from marim_harness.agent import Harness
    from marim_harness.session import SessionManager
    from marim_harness.tools.provider import BuiltinToolProvider

    deps = Deps(workspace_root=tmp_path, mode=Mode.auto)
    manager = SessionManager(tmp_path / "ws", base_dir=tmp_path / "data")
    store = manager.create("headless")
    model = TestModel(call_tools=[], custom_output_text=output_text)
    return Harness(
        model, BuiltinToolProvider(), deps,
        instructions="test", store=store, manager=manager,
    )


@pytest.mark.anyio
async def test_text_format_prints_final_output(tmp_path: Path):
    from marim_harness.interfaces.cli.headless import run_headless

    out = io.StringIO()
    harness = _harness(tmp_path, "the answer is 42")
    code = await run_headless(harness, "what is the answer?", "text", out=out)
    assert code == 0
    assert out.getvalue().strip() == "the answer is 42"


@pytest.mark.anyio
async def test_json_format_emits_structured_object(tmp_path: Path):
    from marim_harness.interfaces.cli.headless import run_headless

    out = io.StringIO()
    harness = _harness(tmp_path, "structured reply")
    code = await run_headless(harness, "go", "json", out=out)
    assert code == 0
    obj = json.loads(out.getvalue())
    assert obj["output"] == "structured reply"
    assert obj["session_id"] == harness.session.store.session_id
    assert obj["name"] == "headless"
    assert set(obj["usage"]) == {"input_tokens", "output_tokens", "total_tokens"}


@pytest.mark.anyio
async def test_stream_json_emits_ndjson_then_result(tmp_path: Path):
    from marim_harness.interfaces.cli.headless import run_headless

    out = io.StringIO()
    harness = _harness(tmp_path, "streamed answer")
    code = await run_headless(harness, "go", "stream-json", out=out)
    assert code == 0

    lines = [json.loads(line) for line in out.getvalue().splitlines() if line.strip()]
    assert lines, "expected at least one event line"
    # every line is a typed event
    assert all("type" in obj for obj in lines)
    # the last line is the terminal result carrying the final output
    assert lines[-1]["type"] == "result"
    assert lines[-1]["output"] == "streamed answer"
    # the streamed text reconstructs the final answer
    text = "".join(o.get("text", "") for o in lines if o["type"] == "text")
    assert "streamed answer" in text


@pytest.mark.anyio
async def test_failed_turn_returns_nonzero_and_writes_stderr(tmp_path: Path):
    from marim_harness.interfaces.cli.headless import run_headless

    out = io.StringIO()
    err = io.StringIO()
    harness = _harness(tmp_path)

    async def boom(*a, **k):
        raise RuntimeError("upstream exploded")

    harness.run_turn = boom  # type: ignore[method-assign]
    code = await run_headless(harness, "go", "text", out=out, err=err)
    assert code == 1
    assert out.getvalue() == ""
    assert "upstream exploded" in err.getvalue()
