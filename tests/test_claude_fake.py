"""The fake ``claude`` speaks the stream-json protocol the real one does —
driven raw here (no marim code) so a broken fake fails this file, not the
process tests that depend on it."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest

from tests.fakes import fake_claude_bin, read_claude_argvs, read_claude_log


async def _drive(
    binary: str, cwd: Path, *lines: dict, timeout: float = 5.0
) -> tuple[list[dict], int, str]:
    proc = await asyncio.create_subprocess_exec(
        binary,
        "--output-format",
        "stream-json",
        cwd=str(cwd),
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    assert proc.stdin is not None and proc.stdout is not None and proc.stderr is not None
    for line in lines:
        proc.stdin.write((json.dumps(line) + "\n").encode())
    await proc.stdin.drain()
    proc.stdin.close()
    out, err = await asyncio.wait_for(proc.communicate(), timeout)
    objs = [json.loads(raw) for raw in out.decode().splitlines() if raw.strip()]
    return objs, proc.returncode or 0, err.decode()


def _init() -> dict:
    return {
        "type": "control_request",
        "request_id": "r1",
        "request": {"subtype": "initialize", "hooks": {}},
    }


def _user(text: str) -> dict:
    return {
        "type": "user",
        "message": {"role": "user", "content": [{"type": "text", "text": text}]},
    }


@pytest.mark.anyio
async def test_text_turn_emits_init_deltas_assistant_and_result(tmp_path: Path):
    binary = fake_claude_bin(tmp_path, {"session_id": "S7", "turns": [[{"text": "Hello"}]]})
    objs, code, _ = await _drive(binary, tmp_path, _init(), _user("hi"))
    assert code == 0
    kinds = [o["type"] for o in objs]
    assert kinds[0] == "control_response"
    assert objs[0]["response"] == {"subtype": "success", "request_id": "r1", "response": {}}
    assert objs[1]["type"] == "user" and objs[1]["isReplay"] is True
    init = objs[2]
    assert init["type"] == "system" and init["subtype"] == "init" and init["session_id"] == "S7"
    deltas = "".join(o["event"]["delta"]["text"] for o in objs if o["type"] == "stream_event")
    assert deltas == "Hello"
    assert objs[-2]["type"] == "assistant"
    assert objs[-1]["type"] == "result" and objs[-1]["result"] == "Hello"
    assert read_claude_argvs(tmp_path) == [["--output-format", "stream-json"]]
    assert [m["type"] for m in read_claude_log(tmp_path)] == ["control_request", "user"]


@pytest.mark.anyio
async def test_can_use_tool_blocks_until_answered_and_records_denial(tmp_path: Path):
    step = {
        "can_use_tool": {"tool_name": "Write", "input": {"file_path": "x"}, "tool_use_id": "tu9"}
    }
    binary = fake_claude_bin(tmp_path, {"turns": [[step]]})
    deny = {
        "type": "control_response",
        "response": {
            "subtype": "success",
            "request_id": "req-1",
            "response": {"behavior": "deny", "message": "plan mode: read-only"},
        },
    }
    objs, _, _ = await _drive(binary, tmp_path, _init(), _user("write"), deny)
    req = next(o for o in objs if o["type"] == "control_request")
    assert req["request_id"] == "req-1"
    assert req["request"]["tool_name"] == "Write" and req["request"]["tool_use_id"] == "tu9"
    tool_result = next(o for o in objs if o["type"] == "user" and not o.get("isReplay"))
    block = tool_result["message"]["content"][0]
    assert block["is_error"] is True and block["content"] == "plan mode: read-only"
    result = objs[-1]
    assert result["type"] == "result" and result["result"] == "denied: plan mode: read-only"
    assert result["permission_denials"][0]["tool_name"] == "Write"


@pytest.mark.anyio
async def test_exit_step_writes_stderr_and_no_result(tmp_path: Path):
    binary = fake_claude_bin(
        tmp_path, {"turns": [[{"text": "a"}, {"exit": {"code": 3, "stderr": "boom"}}]]}
    )
    objs, code, err = await _drive(binary, tmp_path, _init(), _user("go"))
    assert code == 3 and "boom" in err
    assert all(o["type"] != "result" for o in objs)


@pytest.mark.anyio
async def test_unknown_resume_id_fails_before_reading_stdin(tmp_path: Path):
    binary = fake_claude_bin(tmp_path, {"known_sessions": ["S1"], "turns": [[{"text": "a"}]]})
    proc = await asyncio.create_subprocess_exec(
        binary,
        "--resume",
        "NOPE",
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    _, err = await asyncio.wait_for(proc.communicate(), 5.0)
    assert proc.returncode == 1
    assert "No conversation found with session ID: NOPE" in err.decode()
    assert read_claude_log(tmp_path) == []
