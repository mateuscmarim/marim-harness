"""Test doubles for external processes."""

from __future__ import annotations

import json
import stat
import sys
from pathlib import Path

_FAKE_CLAUDE = Path(__file__).with_name("fake_claude.py")


def fake_claude_bin(tmp_path: Path, scenario: dict) -> str:
    """Write ``scenario`` and an executable ``claude`` wrapper into ``tmp_path``;
    return the wrapper path (set it as ``MARIM_CLAUDE_CLI_BIN`` or pass it as
    ``binary=``). One scenario file serves every launch from that ``tmp_path``
    (a resume-retry launches twice)."""
    scenario_path = tmp_path / "claude-scenario.json"
    scenario_path.write_text(json.dumps(scenario))
    log_path = tmp_path / "claude-requests.jsonl"
    wrapper = tmp_path / "claude"
    wrapper.write_text(
        "#!/bin/sh\n"
        f'MARIM_CLAUDE_FAKE_SCENARIO="{scenario_path}" '
        f'MARIM_CLAUDE_FAKE_LOG="{log_path}" '
        f'exec "{sys.executable}" "{_FAKE_CLAUDE}" "$@"\n'
    )
    wrapper.chmod(wrapper.stat().st_mode | stat.S_IEXEC)
    return str(wrapper)


def read_claude_argvs(tmp_path: Path) -> list[list[str]]:
    """Every argv the fake ``claude`` was launched with, oldest first."""
    path = tmp_path / "claude-requests.jsonl.argv"
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]


def read_claude_argv(tmp_path: Path) -> list[str]:
    """The most recent launch's argv (``[]`` when it never launched)."""
    argvs = read_claude_argvs(tmp_path)
    return argvs[-1] if argvs else []


def read_claude_log(tmp_path: Path) -> list[dict]:
    """Every stdin line the fake read, across launches."""
    path = tmp_path / "claude-requests.jsonl"
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]
