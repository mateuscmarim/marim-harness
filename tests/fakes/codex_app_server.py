"""A scripted stand-in for ``codex app-server`` (JSON-RPC over stdio).

Reads a scenario from ``$MARIM_CODEX_FAKE_SCENARIO`` (see Task 3 of the plan
for the schema) and logs every client message to ``$MARIM_CODEX_FAKE_LOG``.
Single-threaded: a turn script runs to completion on the stdin loop, reading
only the replies to its own server requests (and ``turn/interrupt``).
"""

from __future__ import annotations

import json
import os
import sys
import time

DEFAULT_MODELS = [
    {
        "id": "gpt-5.6-sol",
        "model": "gpt-5.6-sol",
        "displayName": "GPT-5.6 Sol",
        "hidden": False,
        "isDefault": True,
        "inputModalities": ["text", "image"],
        "supportedReasoningEfforts": [
            {"reasoningEffort": e} for e in ("low", "medium", "high", "xhigh")
        ],
    },
    {
        "id": "gpt-5.4-mini",
        "model": "gpt-5.4-mini",
        "displayName": "GPT-5.4 mini",
        "hidden": False,
        "isDefault": False,
        "inputModalities": ["text"],
        "supportedReasoningEfforts": [{"reasoningEffort": e} for e in ("low", "medium")],
    },
]


class Fake:
    def __init__(self, scenario: dict, log_path: str) -> None:
        self.scenario = scenario
        self.log = open(log_path, "a")  # noqa: SIM115 - lives for the process
        self.threads = 0
        self.turns = 0
        self.turn_scripts = scenario.get("turns") or [[]]
        self.server_req_n = 0
        self.pending_interrupt = False

    # --- wire helpers -------------------------------------------------------
    def send(self, obj: dict) -> None:
        sys.stdout.write(json.dumps(obj) + "\n")
        sys.stdout.flush()

    def record(self, obj: dict) -> None:
        self.log.write(json.dumps(obj) + "\n")
        self.log.flush()

    def read(self) -> dict | None:
        line = sys.stdin.readline()
        if not line:
            return None
        obj = json.loads(line)
        self.record(obj)
        return obj

    def notify(self, method: str, params: dict) -> None:
        self.send({"method": method, "params": params})

    # --- dispatch -----------------------------------------------------------
    def serve(self) -> None:
        while True:
            msg = self.read()
            if msg is None:
                return
            self.handle(msg)

    def handle(self, msg: dict) -> None:
        method = msg.get("method")
        rid = msg.get("id")
        if method is None:
            return  # a stray reply outside a request step
        if method == "initialized":
            return
        handler = {
            "initialize": self.on_initialize,
            "thread/start": self.on_thread_start,
            "thread/resume": self.on_thread_resume,
            "turn/start": self.on_turn_start,
            "turn/interrupt": self.on_turn_interrupt,
            "turn/steer": self.on_turn_steer,
            "thread/compact/start": self.on_compact,
            "model/list": self.on_model_list,
            "account/rateLimits/read": self.on_rate_limits,
        }.get(method)
        if handler is None:
            self.send({"id": rid, "error": {"code": -32601, "message": f"unknown {method}"}})
            return
        handler(rid, msg.get("params") or {})

    # --- methods ------------------------------------------------------------
    def on_initialize(self, rid, params) -> None:
        self.send(
            {
                "id": rid,
                "result": {
                    "userAgent": self.scenario.get("userAgent", "codex_cli_rs/0.152.1 (fake)"),
                    "codexHome": "/fake/.codex",
                    "platformFamily": "unix",
                    "platformOs": "linux",
                },
            }
        )

    def _thread_obj(self, tid: str, params: dict) -> dict:
        return {
            "thread": {
                "id": tid,
                "cwd": params.get("cwd", "/"),
                "ephemeral": bool(params.get("ephemeral")),
            },
            "model": params.get("model") or "gpt-5.6-sol",
            "modelProvider": "openai",
            "cwd": params.get("cwd", "/"),
            "approvalPolicy": params.get("approvalPolicy", "on-request"),
            "approvalsReviewer": "user",
            "sandbox": params.get("sandbox", "workspace-write"),
        }

    def on_thread_start(self, rid, params) -> None:
        self.threads += 1
        tid = f"thread-{self.threads}"
        result = self._thread_obj(tid, params)
        self.send({"id": rid, "result": result})
        self.notify("thread/started", {"thread": result["thread"]})

    def on_thread_resume(self, rid, params) -> None:
        tid = params.get("threadId", "")
        if tid not in (self.scenario.get("resumable") or []):
            self.send({"id": rid, "error": {"code": -32000, "message": "thread not found"}})
            return
        self.send({"id": rid, "result": self._thread_obj(tid, params)})

    def on_model_list(self, rid, params) -> None:
        self.send(
            {
                "id": rid,
                "result": {
                    "data": self.scenario.get("models", DEFAULT_MODELS),
                    "nextCursor": None,
                },
            }
        )

    def on_rate_limits(self, rid, params) -> None:
        # Scenario key `rateLimits` = the RateLimitSnapshot to return; absent
        # -> an error reply, so the default scenario exercises the
        # failure-is-ignored path of the quota hint.
        limits = self.scenario.get("rateLimits")
        if limits is None:
            self.send({"id": rid, "error": {"code": -32000, "message": "no rate limits"}})
            return
        self.send({"id": rid, "result": {"rateLimits": limits}})

    def on_compact(self, rid, params) -> None:
        self.send({"id": rid, "result": {}})
        self.notify("thread/compacted", {"threadId": params.get("threadId"), "turnId": None})

    def on_turn_steer(self, rid, params) -> None:
        self.send({"id": rid, "result": {"turnId": params.get("expectedTurnId")}})

    def on_turn_interrupt(self, rid, params) -> None:
        # Outside a running script an interrupt is a no-op ack.
        self.pending_interrupt = True
        self.send({"id": rid, "result": {}})

    def on_turn_start(self, rid, params) -> None:
        self.turns += 1
        turn_id = f"turn-{self.turns}"
        tid = params.get("threadId", "thread-1")
        self.send(
            {
                "id": rid,
                "result": {"turn": {"id": turn_id, "status": "inProgress", "items": []}},
            }
        )
        self.notify(
            "turn/started",
            {"threadId": tid, "turn": {"id": turn_id, "status": "inProgress"}},
        )
        script = self.turn_scripts[min(self.turns - 1, len(self.turn_scripts) - 1)]
        status, error = self.run_script(script, tid, turn_id)
        turn = {"id": turn_id, "status": status, "items": [], "error": error}
        self.notify("turn/completed", {"threadId": tid, "turn": turn})

    # --- script execution ---------------------------------------------------
    def _subst(self, value, tid: str, turn_id: str):
        if isinstance(value, str):
            return value.replace("$THREAD", tid).replace("$TURN", turn_id)
        if isinstance(value, dict):
            return {k: self._subst(v, tid, turn_id) for k, v in value.items()}
        if isinstance(value, list):
            return [self._subst(v, tid, turn_id) for v in value]
        return value

    def _params(self, step: dict, tid: str, turn_id: str) -> dict:
        params = dict(self._subst(step.get("params") or {}, tid, turn_id))
        params.setdefault("threadId", tid)
        params.setdefault("turnId", turn_id)
        return params

    def run_script(self, script: list[dict], tid: str, turn_id: str) -> tuple[str, dict | None]:
        self.pending_interrupt = False
        for step in script:
            if "notify" in step:
                self.notify(step["notify"], self._params(step, tid, turn_id))
            elif "request" in step:
                self.server_request(step, tid, turn_id)
            elif step.get("hang"):
                self.wait_for_interrupt()
                return "interrupted", None
            elif "sleep" in step:
                time.sleep(float(step["sleep"]))
            elif "exit" in step:
                sys.stderr.write("fake: dying\n")
                sys.stderr.flush()
                os._exit(int(step["exit"]))
            elif "fail" in step:
                return "failed", {"message": str(step["fail"])}
            if self.pending_interrupt:
                return "interrupted", None
        return "completed", None

    def server_request(self, step: dict, tid: str, turn_id: str) -> None:
        self.server_req_n += 1
        rid = f"srv-{self.server_req_n}"
        self.send(
            {
                "id": rid,
                "method": step["request"],
                "params": self._params(step, tid, turn_id),
            }
        )
        while True:
            msg = self.read()
            if msg is None:
                os._exit(0)
            if msg.get("id") == rid and "method" not in msg:
                self.record({step.get("record_as", "reply"): msg.get("result", msg.get("error"))})
                return
            if msg.get("method") == "turn/interrupt":
                self.on_turn_interrupt(msg.get("id"), msg.get("params") or {})
                continue
            # unrelated request (e.g. model/list) answered inline
            self.handle(msg)

    def wait_for_interrupt(self) -> None:
        while True:
            msg = self.read()
            if msg is None:
                os._exit(0)
            if msg.get("method") == "turn/interrupt":
                self.on_turn_interrupt(msg.get("id"), msg.get("params") or {})
                return
            self.handle(msg)


def main() -> None:
    scenario_path = os.environ.get("MARIM_CODEX_FAKE_SCENARIO")
    scenario = {}
    if scenario_path:
        with open(scenario_path) as f:
            scenario = json.load(f)
    log_path = os.environ.get("MARIM_CODEX_FAKE_LOG") or os.devnull
    if sys.argv[1:] != ["app-server"]:
        sys.stderr.write(f"fake codex: unsupported argv {sys.argv[1:]!r}\n")
        sys.exit(2)
    Fake(scenario, log_path).serve()


if __name__ == "__main__":
    main()
