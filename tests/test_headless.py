import io
import json
import stat
from pathlib import Path

import pytest

from tests.conftest import _make_deps


def _hook_script(tmp_path: Path, name: str, body: str) -> str:
    p = tmp_path / name
    p.write_text("#!/usr/bin/env bash\n" + body, encoding="utf-8")
    p.chmod(p.stat().st_mode | stat.S_IEXEC | stat.S_IRWXU)
    return str(p)


def _harness(tmp_path: Path, output_text: str = "hello from the model", *, hooks=None):
    from pydantic_ai.models.test import TestModel

    from marim_harness.runtime.harness import Harness
    from marim_harness.session import SessionManager
    from marim_harness.tools.provider import BuiltinToolProvider

    deps = _make_deps(tmp_path, hooks=hooks)
    manager = SessionManager(tmp_path / "ws", base_dir=tmp_path / "data")
    store = manager.create("headless")
    model = TestModel(call_tools=[], custom_output_text=output_text)
    from marim_harness.runtime.harness import HarnessConfig

    return Harness(
        model, BuiltinToolProvider(), deps,
        instructions="test",
        config=HarnessConfig(store=store, manager=manager),
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
async def test_headless_settles_background_autoname_before_exit(tmp_path: Path):
    """The turn only *schedules* the autoname; run_headless must await it before
    teardown so the one-shot process reports (and persists) the generated name."""
    import json

    from pydantic_ai.models.test import TestModel

    from marim_harness.interfaces.cli.headless import run_headless
    from marim_harness.runtime.harness import Harness, HarnessConfig
    from marim_harness.session import SessionManager
    from marim_harness.tools.provider import BuiltinToolProvider

    async def titler(messages):
        return "Headless Title"

    deps = _make_deps(tmp_path)
    manager = SessionManager(tmp_path / "ws", base_dir=tmp_path / "data")
    store = manager.create()  # unnamed -> eligible for autonaming
    harness = Harness(
        TestModel(call_tools=[], custom_output_text="done"), BuiltinToolProvider(), deps,
        instructions="test",
        config=HarnessConfig(store=store, manager=manager, titler=titler),
    )

    out = io.StringIO()
    code = await run_headless(harness, "do the thing", "json", out=out)
    assert code == 0
    assert json.loads(out.getvalue())["name"] == "Headless Title"
    assert manager.store(store.session_id).name == "Headless Title"


@pytest.mark.anyio
async def test_headless_command_policy_denylist_blocks_bash(tmp_path: Path, monkeypatch):
    """Regression guard: headless mode wires command_policy through to the bash
    tool. A denylisted command is refused with a clear message, even though the
    same command would succeed if the policy were absent. This protects against
    future refactors that route bash through a different layer and bypass the
    gate."""
    from types import SimpleNamespace

    from marim_harness.command_policy import CommandPolicy
    from marim_harness.tools.edit_tools import bash

    monkeypatch.setenv("MARIM_COMMAND_DENYLIST", "dangerous")
    harness = _harness(tmp_path)
    # Mirror what bootstrap.py does — the env var is loaded into config, then
    # wrapped in CommandPolicy and attached to deps.
    harness.deps.workspace.command_policy = CommandPolicy(denylist=["dangerous"])
    ctx = SimpleNamespace(deps=harness.deps)

    # A bare call to the bash tool with the denylisted command must be blocked.
    out = await bash(ctx, "dangerous --whatever")
    assert "Blocked by command policy" in out
    assert "dangerous" in out

    # A benign command must still pass.
    out2 = await bash(ctx, "echo hello")
    assert "hello" in out2


def test_command_policy_parse_round_trips_env_value():
    """The env-var parsing helper used by bootstrap must round-trip the
    comma-separated value the operator sets in MARIM_COMMAND_DENYLIST."""
    from marim_harness.command_policy import CommandPolicy, split_patterns

    parsed = CommandPolicy.parse(deny="rm -rf, dd if=, mkfs")
    patterns = split_patterns("rm -rf, dd if=, mkfs")
    assert len(parsed._deny) == len(patterns) == 3
    assert parsed.check("rm -rf /") is not None
    assert parsed.check("ls -la") is None


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
    assert set(obj["usage"]) == {
        "input_tokens", "output_tokens", "total_tokens",
        "uncached_input_tokens", "cache_read_tokens", "cache_write_tokens",
        "cost_usd", "cost_is_exact",
    }


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


@pytest.mark.anyio
async def test_headless_fires_session_start_and_end(tmp_path: Path):
    """SessionStart and SessionEnd both fire when run_headless drives a turn."""
    from marim_harness.hooks import events as hook_events
    from marim_harness.hooks.runner import HookRunner
    from marim_harness.interfaces.cli.headless import run_headless

    log = tmp_path / "lifecycle.log"
    helper = tmp_path / "lifecycle_hook.py"
    helper.write_text(
        f"import sys, json\n"
        f"d = json.load(sys.stdin)\n"
        f"open({str(log)!r}, 'a').write(d['hook_event_name'] + '\\n')\n",
        encoding="utf-8",
    )
    cmd = _hook_script(tmp_path, "lifecycle.sh", f"python3 {str(helper)}\n")
    runner = HookRunner({
        hook_events.SESSION_START: [{"hooks": [{"type": "command", "command": cmd}]}],
        hook_events.SESSION_END: [{"hooks": [{"type": "command", "command": cmd}]}],
    })

    out = io.StringIO()
    harness = _harness(tmp_path, "lifecycle reply", hooks=runner)
    code = await run_headless(harness, "hello", "text", out=out)
    assert code == 0

    logged = log.read_text()
    assert "SessionStart" in logged, f"SessionStart not found in hook log: {logged!r}"
    assert "SessionEnd" in logged, f"SessionEnd not found in hook log: {logged!r}"


@pytest.mark.anyio
async def test_finalizes_active_time_and_force_persists_on_shutdown(tmp_path: Path):
    """Headless must close the active-time segment and force a persist before
    teardown, mirroring the TUI — otherwise the final segment is dropped."""
    from marim_harness.interfaces.cli.headless import run_headless

    harness = _harness(tmp_path, "done")
    calls = []
    real_finalize = harness.session.finalize_active_time
    real_persist = harness.session.persist

    def spy_finalize():
        calls.append("finalize")
        return real_finalize()

    def spy_persist(*, force=False):
        calls.append(("persist", force))
        return real_persist(force=force)

    harness.session.finalize_active_time = spy_finalize  # type: ignore[method-assign]
    harness.session.persist = spy_persist  # type: ignore[method-assign]

    out = io.StringIO()
    code = await run_headless(harness, "go", "text", out=out)
    assert code == 0
    # finalize ran, and a forced persist followed it (order matters: finalize
    # closes the segment so the forced persist counts it exactly once). A first
    # turn also force-writes a baseline persist at turn *start*, so assert on a
    # forced persist that comes AFTER finalize, not merely the first one.
    assert "finalize" in calls
    finalize_at = calls.index("finalize")
    assert any(c == ("persist", True) and i > finalize_at for i, c in enumerate(calls))


@pytest.mark.anyio
async def test_cleanup_error_does_not_mask_turn_failure(tmp_path: Path):
    """A raising teardown step must not swallow the real turn error or flip the
    exit code from failure (1) back to success."""
    from marim_harness.interfaces.cli.headless import run_headless

    out = io.StringIO()
    err = io.StringIO()
    harness = _harness(tmp_path)

    async def boom(*a, **k):
        raise RuntimeError("upstream exploded")

    async def session_end_boom(*a, **k):
        raise RuntimeError("cleanup also exploded")

    harness.run_turn = boom  # type: ignore[method-assign]
    harness.session_end = session_end_boom  # type: ignore[method-assign]

    code = await run_headless(harness, "go", "text", out=out, err=err)
    assert code == 1  # primary failure exit code survives the cleanup error
    assert "upstream exploded" in err.getvalue()  # primary error surfaced
    assert "cleanup also exploded" not in err.getvalue()  # cleanup error stays out of band


@pytest.mark.anyio
async def test_cleanup_error_on_success_does_not_break(tmp_path: Path):
    """A raising teardown on an otherwise-successful turn must not flip the exit
    code or lose the result output."""
    from marim_harness.interfaces.cli.headless import run_headless

    harness = _harness(tmp_path, "the answer is 42")

    async def aclose_boom():
        raise RuntimeError("aclose exploded")

    harness.aclose = aclose_boom  # type: ignore[method-assign]

    out = io.StringIO()
    code = await run_headless(harness, "go", "text", out=out)
    assert code == 0
    assert out.getvalue().strip() == "the answer is 42"


@pytest.mark.anyio
async def test_stream_json_emits_terminal_error_line_on_failure(tmp_path: Path):
    """stream-json consumers parse NDJSON; a crashed turn must still emit a
    terminal error line so the stream isn't silently truncated."""
    from marim_harness.interfaces.cli.headless import run_headless

    out = io.StringIO()
    err = io.StringIO()
    harness = _harness(tmp_path)

    async def boom(*a, **k):
        raise RuntimeError("upstream exploded")

    harness.run_turn = boom  # type: ignore[method-assign]
    code = await run_headless(harness, "go", "stream-json", out=out, err=err)
    assert code == 1

    lines = [json.loads(line) for line in out.getvalue().splitlines() if line.strip()]
    assert lines, "expected a terminal error line on stdout"
    assert lines[-1]["type"] == "error"
    assert "upstream exploded" in lines[-1]["error"]
    # and the human-readable error still goes to stderr
    assert "upstream exploded" in err.getvalue()
