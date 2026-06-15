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
