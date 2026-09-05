from __future__ import annotations

import json
import subprocess
import sys

from tests.fakes import fake_codex_bin, read_request_log


def _run(binary: str, messages: list[dict]) -> list[dict]:
    payload = "".join(json.dumps(m) + "\n" for m in messages)
    proc = subprocess.run(
        [binary, "app-server"], input=payload, capture_output=True, text=True, timeout=10
    )
    return [json.loads(line) for line in proc.stdout.splitlines() if line.strip()]


def test_fake_initialize_thread_and_turn(tmp_path):
    scenario = {
        "turns": [
            [
                {"notify": "item/agentMessage/delta", "params": {"itemId": "m1", "delta": "hel"}},
                {"notify": "item/agentMessage/delta", "params": {"itemId": "m1", "delta": "lo"}},
            ]
        ]
    }
    binary = fake_codex_bin(tmp_path, scenario)
    out = _run(
        binary,
        [
            {
                "id": 1,
                "method": "initialize",
                "params": {"clientInfo": {"name": "t", "version": "0"}},
            },
            {"method": "initialized"},
            {"id": 2, "method": "thread/start", "params": {"cwd": "/w"}},
            {
                "id": 3,
                "method": "turn/start",
                "params": {"threadId": "thread-1", "input": [{"type": "text", "text": "hi"}]},
            },
        ],
    )
    by_id = {o["id"]: o for o in out if "id" in o}
    assert by_id[1]["result"]["userAgent"].startswith("codex_cli_rs/0.152.1")
    assert by_id[2]["result"]["thread"]["id"] == "thread-1"
    assert by_id[3]["result"]["turn"]["id"] == "turn-1"
    methods = [o["method"] for o in out if "method" in o]
    assert methods == [
        "thread/started",
        "turn/started",
        "item/agentMessage/delta",
        "item/agentMessage/delta",
        "turn/completed",
    ]
    deltas = [o["params"] for o in out if o.get("method") == "item/agentMessage/delta"]
    assert deltas[0]["threadId"] == "thread-1" and deltas[0]["turnId"] == "turn-1"
    done = next(o for o in out if o.get("method") == "turn/completed")
    assert done["params"]["turn"]["status"] == "completed"
    log = read_request_log(tmp_path)
    assert [m["method"] for m in log] == ["initialize", "initialized", "thread/start", "turn/start"]


def test_fake_request_step_blocks_for_reply(tmp_path):
    scenario = {
        "turns": [
            [
                {
                    "request": "item/commandExecution/requestApproval",
                    "params": {"itemId": "c1", "command": "ls"},
                    "record_as": "approval",
                }
            ]
        ]
    }
    binary = fake_codex_bin(tmp_path, scenario)
    out = _run(
        binary,
        [
            {
                "id": 1,
                "method": "initialize",
                "params": {"clientInfo": {"name": "t", "version": "0"}},
            },
            {"id": 2, "method": "thread/start", "params": {}},
            {
                "id": 3,
                "method": "turn/start",
                "params": {"threadId": "thread-1", "input": []},
            },
            {"id": "srv-1", "result": {"decision": "accept"}},
        ],
    )
    req = next(o for o in out if o.get("method") == "item/commandExecution/requestApproval")
    assert req["id"] == "srv-1"
    log = read_request_log(tmp_path)
    assert any(m.get("approval") == {"decision": "accept"} for m in log)


def test_fake_resume_unknown_thread_errors(tmp_path):
    binary = fake_codex_bin(tmp_path, {"resumable": ["thread-9"]})
    out = _run(
        binary,
        [
            {"id": 1, "method": "thread/resume", "params": {"threadId": "thread-9"}},
            {"id": 2, "method": "thread/resume", "params": {"threadId": "thread-8"}},
        ],
    )
    by_id = {o["id"]: o for o in out if "id" in o}
    assert by_id[1]["result"]["thread"]["id"] == "thread-9"
    assert by_id[2]["error"]["code"] == -32000


def test_fake_exit_step_dies(tmp_path):
    binary = fake_codex_bin(tmp_path, {"turns": [[{"exit": 3}]]})
    proc = subprocess.run(
        [binary, "app-server"],
        input=(
            json.dumps({"id": 1, "method": "thread/start", "params": {}})
            + "\n"
            + json.dumps(
                {
                    "id": 2,
                    "method": "turn/start",
                    "params": {"threadId": "thread-1", "input": []},
                }
            )
            + "\n"
        ),
        capture_output=True,
        text=True,
        timeout=10,
    )
    assert proc.returncode == 3
    assert "fake: dying" in proc.stderr


def test_wrapper_uses_current_interpreter(tmp_path):
    binary = fake_codex_bin(tmp_path, {})
    with open(binary) as f:
        assert sys.executable in f.read()
