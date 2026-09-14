"""Scenario-driven fake ``claude`` for tests (bidirectional stream-json).

Speaks the subset of Claude Code's stream-json control protocol that
``marim_harness.claude.process.ClaudeProcess`` uses: answers ``initialize`` /
``interrupt`` / any other control_request with an empty success, echoes user
messages with ``isReplay``, emits ``system/init`` after the first user message,
and runs one scripted turn per user message. Nothing here spawns anything —
every emitted object is scripted by the scenario JSON (see the table in the
plan / ``tests.fakes.fake_claude_bin``).

Env: ``MARIM_CLAUDE_FAKE_SCENARIO`` (scenario JSON path, required) and
``MARIM_CLAUDE_FAKE_LOG`` (every stdin line is appended here; argv is appended
as one JSON line to ``<log>.argv`` per launch).
"""

from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path


def _user_text(msg: dict) -> str:
    content = (msg.get("message") or {}).get("content")
    if isinstance(content, str):
        return content
    parts = [c.get("text", "") for c in content or [] if isinstance(c, dict)]
    return "".join(parts)


class Fake:
    def __init__(self, scenario: dict, log_path: str | None) -> None:
        self.scenario = scenario
        self.log_path = log_path
        self.session_id = scenario.get("session_id", "S1")
        self.model = scenario.get("model", "claude-sonnet-4-6")
        self.version = scenario.get("version", "2.1.261")
        self.turn_n = 0
        self.init_sent = False
        self.ended = False  # the current turn was ended by a step (exit/abort)
        self.denials: list[dict] = []
        self._request_n = 0

    # --- wire ----------------------------------------------------------------
    def send(self, obj: dict) -> None:
        sys.stdout.write(json.dumps(obj) + "\n")
        sys.stdout.flush()

    def record(self, line: str) -> None:
        if self.log_path:
            with open(self.log_path, "a", encoding="utf-8") as fh:
                fh.write(line if line.endswith("\n") else line + "\n")

    def read(self) -> dict | None:
        line = sys.stdin.readline()
        if not line:
            return None
        self.record(line)
        try:
            obj = json.loads(line)
        except ValueError:
            return {}
        return obj if isinstance(obj, dict) else {}

    def respond(self, request_id: str, body: dict) -> None:
        self.send(
            {
                "type": "control_response",
                "response": {"subtype": "success", "request_id": request_id, "response": body},
            }
        )

    def fail(self, request_id: str, error: str) -> None:
        self.send(
            {
                "type": "control_response",
                "response": {"subtype": "error", "request_id": request_id, "error": error},
            }
        )

    def _control_error(self, msg: dict) -> str | None:
        request = msg.get("request") or {}
        if request.get("subtype") != "set_model":
            return None
        if request.get("model") in (self.scenario.get("reject_models") or []):
            return f"Model '{request['model']}' not found"
        return None

    def _control_body(self, msg: dict) -> dict:
        """The between-turns control answers the scenario can script:
        ``initialize`` carries the model menu (``models``), ``get_usage`` the
        rate-limit reading (``usage_report``); anything else an empty
        success."""
        subtype = (msg.get("request") or {}).get("subtype")
        if subtype == "initialize" and "models" in self.scenario:
            return {"models": self.scenario["models"]}
        if subtype == "get_usage" and "usage_report" in self.scenario:
            return self.scenario["usage_report"]
        return {}

    def _assistant(self, content: list[dict]) -> dict:
        return {
            "type": "assistant",
            "message": {"role": "assistant", "model": self.model, "content": content},
            "session_id": self.session_id,
        }

    def _tool_result(self, tool_use_id: str, content: str, is_error: bool) -> dict:
        block = {"type": "tool_result", "tool_use_id": tool_use_id, "content": content}
        if is_error:
            block["is_error"] = True
        return {
            "type": "user",
            "message": {"role": "user", "content": [block]},
            "session_id": self.session_id,
        }

    def _delta(self, delta: dict) -> dict:
        return {
            "type": "stream_event",
            "event": {"type": "content_block_delta", "index": 0, "delta": delta},
            "session_id": self.session_id,
        }

    def _result(self, text: str, overrides: dict) -> dict:
        base = {
            "type": "result",
            "subtype": "success",
            "is_error": False,
            "result": text,
            "session_id": self.session_id,
            "num_turns": 1,
            "duration_ms": 5,
            "total_cost_usd": 0.001,
            "usage": {
                "input_tokens": 7,
                "output_tokens": 4,
                "cache_read_input_tokens": 0,
                "cache_creation_input_tokens": 0,
            },
            "permission_denials": list(self.denials),
        }
        base.update(overrides)
        return base

    def _aborted_result(self) -> dict:
        return {
            "type": "result",
            "subtype": "error_during_execution",
            "is_error": True,
            "terminal_reason": "aborted_streaming",
            "session_id": self.session_id,
            "num_turns": 1,
            "usage": {"input_tokens": 1, "output_tokens": 1},
            "permission_denials": [],
        }

    # --- main loop -----------------------------------------------------------
    def serve(self) -> None:
        while True:
            msg = self.read()
            if msg is None:
                return
            kind = msg.get("type")
            if kind == "control_request":
                # Between turns every control request (initialize, set_model,
                # a late interrupt) gets an empty success — nothing to abort.
                # `initialize` alone carries the scenario's model menu when
                # one is given, the way the real CLI's handshake does; a
                # `set_model` naming one of the scenario's `reject_models`
                # gets the real CLI's error reply instead.
                error = self._control_error(msg)
                if error is not None:
                    self.fail(msg["request_id"], error)
                else:
                    self.respond(msg["request_id"], self._control_body(msg))
            elif kind == "user" and not msg.get("isReplay"):
                self.run_turn(msg)

    def run_turn(self, msg: dict) -> None:
        self.send({**msg, "isReplay": True})
        if not self.init_sent:
            self.init_sent = True
            self.send(
                {
                    "type": "system",
                    "subtype": "init",
                    "session_id": self.session_id,
                    "model": self.model,
                    "claude_code_version": self.version,
                    "tools": ["Read", "Write", "Edit", "Bash"],
                    "cwd": os.getcwd(),
                }
            )
        turns = self.scenario.get("turns") or [[{"text": "ok"}]]
        script = turns[min(self.turn_n, len(turns) - 1)]
        self.turn_n += 1
        self.ended = False
        self.denials = []
        overrides: dict = {}
        text_parts: list[str] = []
        for step in script:
            if self.ended:
                break
            self.step(step, text_parts, overrides)
        if not self.ended:
            self.send(self._result("".join(text_parts), overrides))

    # --- steps ---------------------------------------------------------------
    # A dispatch table (rather than an if/elif chain) so adding a step kind
    # never touches this method's branch count — each handler carries its own
    # single concern and the cyclomatic weight stays on the loop, not here.
    def step(self, step: dict, text_parts: list[str], overrides: dict) -> None:
        handlers = {
            "text": self._step_text,
            "thinking": self._step_thinking,
            "tool_use": self._step_tool_use,
            "tool_result": self._step_tool_result,
            "can_use_tool": self._step_can_use_tool,
            "await_user": self._step_await_user,
            "await_interrupt": self._step_await_interrupt,
            "sleep": self._step_sleep,
            "exit": self._step_exit,
            "raw": self._step_raw,
            "result": self._step_result,
        }
        for key, handler in handlers.items():
            if key in step:
                handler(step[key], text_parts, overrides)
                return
        raise SystemExit(f"fake_claude: unknown step {step!r}")

    def _step_text(self, text: str, text_parts: list[str], overrides: dict) -> None:
        self.emit_text(text)
        text_parts.append(text)

    def _step_thinking(self, thinking: str, text_parts: list[str], overrides: dict) -> None:
        self.send(self._delta({"type": "thinking_delta", "thinking": thinking}))
        self.send(self._assistant([{"type": "thinking", "thinking": thinking}]))

    def _step_tool_use(self, t: dict, text_parts: list[str], overrides: dict) -> None:
        self.send(
            self._assistant(
                [
                    {
                        "type": "tool_use",
                        "id": t["id"],
                        "name": t["name"],
                        "input": t.get("input", {}),
                    }
                ]
            )
        )

    def _step_tool_result(self, r: dict, text_parts: list[str], overrides: dict) -> None:
        self.send(self._tool_result(r["id"], r.get("content", ""), bool(r.get("is_error"))))

    def _step_can_use_tool(self, spec: dict, text_parts: list[str], overrides: dict) -> None:
        self.prompt(spec, text_parts)

    def _step_await_user(self, _value: bool, text_parts: list[str], overrides: dict) -> None:
        self.await_user(text_parts)

    def _step_await_interrupt(self, _value: bool, text_parts: list[str], overrides: dict) -> None:
        self.await_interrupt()

    def _step_sleep(self, seconds: float, text_parts: list[str], overrides: dict) -> None:
        time.sleep(float(seconds))

    def _step_exit(self, e: dict, text_parts: list[str], overrides: dict) -> None:
        sys.stdout.flush()
        sys.stderr.write(str(e.get("stderr", "")) + "\n")
        sys.stderr.flush()
        sys.exit(int(e.get("code", 1)))

    def _step_raw(self, raw: dict, text_parts: list[str], overrides: dict) -> None:
        self.send(raw)

    def _step_result(self, result: dict, text_parts: list[str], overrides: dict) -> None:
        overrides.update(result)

    def emit_text(self, text: str) -> None:
        for i in range(0, len(text), 3):
            self.send(self._delta({"type": "text_delta", "text": text[i : i + 3]}))
        self.send(self._assistant([{"type": "text", "text": text}]))

    def prompt(self, spec: dict, text_parts: list[str]) -> None:
        self._request_n += 1
        rid = f"req-{self._request_n}"
        tool_name = spec["tool_name"]
        tool_input = spec.get("input", {})
        tid = spec.get("tool_use_id", "tu1")
        request = {
            "subtype": "can_use_tool",
            "tool_name": tool_name,
            "input": tool_input,
            "tool_use_id": tid,
            "permission_suggestions": [],
        }
        if spec.get("requires_user_interaction"):
            request["requires_user_interaction"] = True
        self.send(
            self._assistant(
                [{"type": "tool_use", "id": tid, "name": tool_name, "input": tool_input}]
            )
        )
        self.send({"type": "control_request", "request_id": rid, "request": request})
        reply = self.await_response(rid)
        if reply is None:
            return  # interrupted (aborted result already sent) or EOF
        if reply.get("behavior") == "allow":
            answers = (reply.get("updatedInput") or {}).get("answers")
            if answers is None:
                self.send(self._tool_result(tid, "ok", False))
                text = f"{tool_name} done"
            else:
                text = "answers=" + json.dumps(answers, sort_keys=True)
                self.send(self._tool_result(tid, text, False))
        else:
            message = str(reply.get("message") or "denied")
            self.send(self._tool_result(tid, message, True))
            self.denials.append(
                {"tool_name": tool_name, "tool_use_id": tid, "tool_input": tool_input}
            )
            text = f"denied: {message}"
        self.emit_text(text)
        text_parts.append(text)

    def await_response(self, rid: str) -> dict | None:
        while True:
            msg = self.read()
            if msg is None:
                self.ended = True
                return None
            if msg.get("type") == "control_response":
                resp = msg.get("response") or {}
                if resp.get("request_id") != rid:
                    continue
                if resp.get("subtype") == "error":
                    return {"behavior": "deny", "message": resp.get("error") or "error"}
                return resp.get("response") or {}
            if msg.get("type") == "control_request":
                self.handle_control(msg, pending=rid)
                if self.ended:
                    return None

    def await_user(self, text_parts: list[str]) -> None:
        while True:
            msg = self.read()
            if msg is None:
                self.ended = True
                return
            if msg.get("type") == "user" and not msg.get("isReplay"):
                self.send({**msg, "isReplay": True})
                text = "heard: " + _user_text(msg)
                self.emit_text(text)
                text_parts.append(text)
                return
            if msg.get("type") == "control_request":
                self.handle_control(msg, pending=None)
                if self.ended:
                    return

    def await_interrupt(self) -> None:
        while True:
            msg = self.read()
            if msg is None:
                self.ended = True
                return
            if msg.get("type") == "control_request":
                self.handle_control(msg, pending=None)
                if self.ended:
                    return

    def handle_control(self, msg: dict, pending: str | None) -> None:
        subtype = (msg.get("request") or {}).get("subtype")
        if subtype != "interrupt":
            self.respond(msg["request_id"], {})
            return
        if pending is not None:
            self.send({"type": "control_cancel_request", "request_id": pending})
        self.respond(msg["request_id"], {})
        self.send(self._aborted_result())
        self.ended = True


def main(argv: list[str]) -> int:
    # Record the real pid (the wrapper `exec`s us, so this is the pid the
    # parent spawned) next to the log, for tests that assert the process was
    # killed. sys.argv[0] is this module, not the wrapper — hence the log path.
    Path(os.environ["MARIM_CLAUDE_FAKE_LOG"]).with_name("claude.pid").write_text(str(os.getpid()))
    scenario = json.loads(
        Path(os.environ["MARIM_CLAUDE_FAKE_SCENARIO"]).read_text(encoding="utf-8")
    )
    log = os.environ.get("MARIM_CLAUDE_FAKE_LOG")
    if log:
        with open(log + ".argv", "a", encoding="utf-8") as fh:
            fh.write(json.dumps(argv) + "\n")
    if "--resume" in argv:
        rid = argv[argv.index("--resume") + 1]
        if rid not in (scenario.get("known_sessions") or []):
            sys.stderr.write(f"No conversation found with session ID: {rid}\n")
            sys.stderr.flush()
            return 1
        # A resume continues the same session unless the scenario pinned an id
        # of its own — that models a fork, where the CLI mints a NEW session id
        # and the caller must key later resumes off it.
        scenario.setdefault("session_id", rid)
    Fake(scenario, log).serve()
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
