from pathlib import Path

import pytest

from marim_harness.tools import shell


@pytest.mark.anyio
async def test_bash_captures_stdout(tmp_path: Path):
    out = await shell.run_bash(tmp_path, "echo hello")
    assert "hello" in out
    assert "exit 0" in out


@pytest.mark.anyio
async def test_bash_runs_in_workspace_cwd(tmp_path: Path):
    (tmp_path / "marker.txt").write_text("")
    out = await shell.run_bash(tmp_path, "ls")
    assert "marker.txt" in out


@pytest.mark.anyio
async def test_bash_times_out(tmp_path: Path):
    out = await shell.run_bash(tmp_path, "sleep 5", timeout=1)
    assert "timed out" in out


@pytest.mark.anyio
async def test_bash_truncates_long_output(tmp_path: Path):
    out = await shell.run_bash(
        tmp_path, "for i in $(seq 1 5000); do echo line$i; done", max_output=200
    )
    assert "(truncated)" in out


@pytest.mark.anyio
async def test_start_bash_streams_buffer_then_final(tmp_path: Path):
    bp = await shell.start_bash(tmp_path, "echo one; echo two")
    final = await bp.wait()
    assert "one" in bp.output() and "two" in bp.output()  # live buffer filled
    assert "one" in final and "two" in final
    assert "exit 0" in final


@pytest.mark.anyio
async def test_start_bash_kill_stops_process(tmp_path: Path):
    bp = await shell.start_bash(tmp_path, "sleep 30")
    bp.kill()
    final = await bp.wait()  # returns promptly once killed, not after 30s
    assert "exit" in final
    assert bp.returncode is not None
