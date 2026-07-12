from types import SimpleNamespace

import pytest

from marim_harness.tools.workflow_tools import run_workflow
from tests.conftest import _make_deps


def _ctx(deps, tool_call_id="tc1"):
    return SimpleNamespace(deps=deps, tool_call_id=tool_call_id)


@pytest.mark.anyio
async def test_unavailable_seam_returns_install_hint(tmp_path):
    deps = _make_deps(tmp_path)
    deps.services.run_workflow = None
    out = await run_workflow(_ctx(deps), "1 + 1")
    assert "workflows" in out.lower() and "install" in out.lower()


@pytest.mark.anyio
async def test_delegates_script_args_and_tool_call_id(tmp_path):
    deps = _make_deps(tmp_path)
    seen = {}

    async def fake_runner(script, args, tool_call_id, timeout_secs):
        seen.update(script=script, args=args, tool_call_id=tool_call_id,
                    timeout_secs=timeout_secs)
        return "result"

    deps.services.run_workflow = fake_runner
    out = await run_workflow(_ctx(deps, "abc"), "1 + 1", args={"k": 1})
    assert out == "result"
    assert seen == {"script": "1 + 1", "args": {"k": 1}, "tool_call_id": "abc",
                    "timeout_secs": None}


@pytest.mark.anyio
async def test_timeout_secs_is_forwarded(tmp_path):
    deps = _make_deps(tmp_path)
    seen = {}

    async def fake_runner(script, args, tool_call_id, timeout_secs):
        seen["timeout_secs"] = timeout_secs
        return "ok"

    deps.services.run_workflow = fake_runner
    await run_workflow(_ctx(deps), "1 + 1", timeout_secs=1800.0)
    assert seen["timeout_secs"] == 1800.0


@pytest.mark.anyio
async def test_invalid_timeout_is_rejected_without_running(tmp_path):
    """<=0 or non-finite timeouts are a model mistake: answer with a
    correctable error and never start the VM."""
    deps = _make_deps(tmp_path)

    async def fake_runner(*a):
        raise AssertionError("runner must not be called")

    deps.services.run_workflow = fake_runner
    for bad in (0.0, -5.0, float("inf"), float("nan")):
        out = await run_workflow(_ctx(deps), "1 + 1", timeout_secs=bad)
        assert "timeout_secs" in out and "positive" in out


@pytest.mark.anyio
async def test_stringified_container_args_are_decoded(tmp_path):
    """Models routinely JSON-encode the args parameter — JsonValue accepts the
    string, so a script's `for q in args` iterates CHARACTERS. A live deep-research
    run spawned one researcher per character of '["What is...' this way, and an
    earlier run crashed on args["diff_path"] for the same reason. A string that
    parses to a dict/list is unambiguously what the model meant: decode it."""
    deps = _make_deps(tmp_path)
    seen = {}

    async def fake_runner(script, args, tool_call_id, timeout_secs):
        seen["args"] = args
        return "ok"

    deps.services.run_workflow = fake_runner
    await run_workflow(_ctx(deps), "args", args='["q1", "q2"]')
    assert seen["args"] == ["q1", "q2"]
    await run_workflow(_ctx(deps), "args", args='{"diff_path": "x.diff"}')
    assert seen["args"] == {"diff_path": "x.diff"}
    # Whitespace padding must not defeat the decode.
    await run_workflow(_ctx(deps), "args", args='  ["q"]\n')
    assert seen["args"] == ["q"]


@pytest.mark.anyio
async def test_non_container_string_args_pass_through_verbatim(tmp_path):
    """Only strings that decode to a JSON container are rewritten. A plain
    topic string, a JSON scalar ('42', '"quoted"'), and malformed near-JSON
    all pass through untouched — the script asked for a string and got one."""
    deps = _make_deps(tmp_path)
    seen = {}

    async def fake_runner(script, args, tool_call_id, timeout_secs):
        seen["args"] = args
        return "ok"

    deps.services.run_workflow = fake_runner
    for raw in ("recursive language models", '"quoted"', "42", "[not json", ""):
        await run_workflow(_ctx(deps), "args", args=raw)
        assert seen["args"] == raw
    # Real containers are untouched too — no double handling.
    await run_workflow(_ctx(deps), "args", args=["already", "a", "list"])
    assert seen["args"] == ["already", "a", "list"]


def test_docstring_warns_about_common_mistakes():
    """The run_workflow docstring is the model-facing product doc for the
    sandbox dialect; the common-mistakes section was added from failures
    observed in live runs, so a future rewrite must not silently drop it."""
    doc = run_workflow.__doc__ or ""
    assert "Common mistakes" in doc
    assert "print(result)" in doc
    assert "asyncio.run" in doc
    assert "log()" in doc
    assert "timeout_secs" in doc
    # args must be a real JSON value; the stringified form burned a live run.
    assert "JSON-encoded" in doc
