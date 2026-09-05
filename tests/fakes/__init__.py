"""Test doubles for external processes."""

from __future__ import annotations

import json
import stat
import sys
from pathlib import Path

_FAKE = Path(__file__).with_name("codex_app_server.py")


def fake_codex_bin(tmp_path: Path, scenario: dict) -> str:
    """Write ``scenario`` and an executable ``codex`` wrapper into ``tmp_path``;
    return the wrapper path (set it as ``MARIM_CODEX_CLI_BIN``)."""
    scenario_path = tmp_path / "codex-scenario.json"
    scenario_path.write_text(json.dumps(scenario))
    log_path = tmp_path / "codex-requests.jsonl"
    wrapper = tmp_path / "codex"
    wrapper.write_text(
        "#!/bin/sh\n"
        f'MARIM_CODEX_FAKE_SCENARIO="{scenario_path}" '
        f'MARIM_CODEX_FAKE_LOG="{log_path}" '
        f'exec "{sys.executable}" "{_FAKE}" "$@"\n'
    )
    wrapper.chmod(wrapper.stat().st_mode | stat.S_IEXEC)
    return str(wrapper)


def read_argv(tmp_path: Path) -> list[str]:
    """The argv the fake ``app-server`` process was launched with."""
    path = tmp_path / "codex-requests.jsonl.argv"
    return json.loads(path.read_text()) if path.exists() else []


def read_request_log(tmp_path: Path) -> list[dict]:
    path = tmp_path / "codex-requests.jsonl"
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]
