"""Launch-time attach discovery (phase 4a): ``server/attach.py``.

``discover`` runs right after ``try_acquire`` refused a session. It decides
between "attach to the daemon that owns it" and "print the usual refusal,
with the reason the attach was skipped". Every network read goes through the
injected ``fetch``; the one real HTTP helper is exercised against a tiny
stdlib server.
"""

from __future__ import annotations

import json
import os
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

from marim_harness.server.attach import (
    AttachDecision,
    RemoteTarget,
    discover,
    read_token,
    urllib_fetch,
)
from marim_harness.server.runtime import write_runtime
from marim_harness.session.claim import try_acquire

ENDPOINT = "http://127.0.0.1:8642"


def _sidecar(session_path: Path, *, kind: str, endpoint: str | None, pid: int | None = None):
    """Write the claim sidecar directly — ``read_holder`` reads it whether or
    not the lock is held, which is exactly the launch's situation."""
    session_path.parent.mkdir(parents=True, exist_ok=True)
    session_path.write_text("{}")
    Path(f"{session_path}.claim").write_text(
        json.dumps(
            {"pid": pid if pid is not None else os.getpid(), "kind": kind, "endpoint": endpoint}
        )
    )


def _state_dir(tmp_path: Path, *, token: str | None = "tok", runtime_pid: bool = True) -> Path:
    state = tmp_path / "state"
    state.mkdir(exist_ok=True)
    if token is not None:
        (state / "token").write_text(token + "\n")
    if runtime_pid:
        write_runtime(state, host="127.0.0.1", port=8642)
    return state


class _Fetch:
    """A scripted ``fetch``: URL → body, recording every call with its token."""

    def __init__(self, **bodies) -> None:
        self.bodies = {f"{ENDPOINT}/v1/{k}": v for k, v in bodies.items()}
        self.calls: list[tuple[str, str | None]] = []

    def __call__(self, url: str, token: str | None):
        self.calls.append((url, token))
        return self.bodies.get(url)


def _workspaces(*paths: Path) -> dict:
    return {"workspaces": [{"id": f"ws{i}", "path": str(p)} for i, p in enumerate(paths)]}


def test_non_daemon_holder_is_not_an_attach_candidate(tmp_path):
    session = tmp_path / "sessions" / "s.json"
    _sidecar(session, kind="tui", endpoint=None)
    fetch = _Fetch()
    decision = discover(tmp_path, "s", session, state_dir=_state_dir(tmp_path), fetch=fetch)
    assert decision == AttachDecision()
    assert fetch.calls == []  # nothing probed for a TUI's claim


def test_no_sidecar_at_all_is_not_an_attach_candidate(tmp_path):
    session = tmp_path / "sessions" / "s.json"
    decision = discover(tmp_path, "s", session, state_dir=_state_dir(tmp_path), fetch=_Fetch())
    assert decision.target is None and decision.reason is None


def test_daemon_claim_without_endpoint_has_a_reason(tmp_path):
    session = tmp_path / "sessions" / "s.json"
    _sidecar(session, kind="daemon", endpoint=None)
    decision = discover(tmp_path, "s", session, state_dir=_state_dir(tmp_path), fetch=_Fetch())
    assert decision.target is None
    assert decision.reason == "its claim names no endpoint"


def test_unreachable_daemon_has_a_reason(tmp_path):
    session = tmp_path / "sessions" / "s.json"
    _sidecar(session, kind="daemon", endpoint=ENDPOINT + "/")
    fetch = _Fetch()  # health → None
    decision = discover(tmp_path, "s", session, state_dir=_state_dir(tmp_path), fetch=fetch)
    assert decision.reason == f"the daemon at {ENDPOINT} is not answering"
    assert fetch.calls == [(f"{ENDPOINT}/v1/health", None)]  # health is unauthenticated


def test_stale_claim_from_another_daemon_pid_has_a_reason(tmp_path):
    session = tmp_path / "sessions" / "s.json"
    _sidecar(session, kind="daemon", endpoint=ENDPOINT, pid=424242)
    fetch = _Fetch(health={"ok": True})
    decision = discover(tmp_path, "s", session, state_dir=_state_dir(tmp_path), fetch=fetch)
    assert decision.target is None
    assert decision.reason is not None
    assert "pid 424242" in decision.reason and f"pid {os.getpid()}" in decision.reason


def test_missing_runtime_record_does_not_block(tmp_path):
    """No runtime.json (a daemon started by hand, or an older one): the pid
    check is skipped rather than failed."""
    session = tmp_path / "sessions" / "s.json"
    _sidecar(session, kind="daemon", endpoint=ENDPOINT, pid=424242)
    state = _state_dir(tmp_path, runtime_pid=False)
    fetch = _Fetch(health={"ok": True}, workspaces=_workspaces(tmp_path))
    decision = discover(tmp_path, "s", session, state_dir=state, fetch=fetch)
    assert decision.target is not None


def test_unreadable_token_has_a_reason(tmp_path):
    session = tmp_path / "sessions" / "s.json"
    _sidecar(session, kind="daemon", endpoint=ENDPOINT)
    state = _state_dir(tmp_path, token=None)
    fetch = _Fetch(health={"ok": True})
    decision = discover(tmp_path, "s", session, state_dir=state, fetch=fetch)
    assert decision.reason == f"no readable token at {state / 'token'}"


def test_refused_token_has_a_reason(tmp_path):
    session = tmp_path / "sessions" / "s.json"
    _sidecar(session, kind="daemon", endpoint=ENDPOINT)
    fetch = _Fetch(health={"ok": True})  # workspaces → None
    decision = discover(tmp_path, "s", session, state_dir=_state_dir(tmp_path), fetch=fetch)
    assert decision.reason == "the daemon refused the token (GET /v1/workspaces failed)"
    assert fetch.calls[-1] == (f"{ENDPOINT}/v1/workspaces", "tok")


def test_unregistered_workspace_has_a_reason(tmp_path):
    session = tmp_path / "sessions" / "s.json"
    _sidecar(session, kind="daemon", endpoint=ENDPOINT)
    other = tmp_path / "elsewhere"
    fetch = _Fetch(
        health={"ok": True},
        workspaces={"workspaces": [{"id": "bad"}, {"path": 3}, *_workspaces(other)["workspaces"]]},
    )
    decision = discover(tmp_path, "s", session, state_dir=_state_dir(tmp_path), fetch=fetch)
    assert decision.reason == f"{tmp_path} is not a registered workspace on the daemon"


def test_attachable_daemon_yields_a_target(tmp_path):
    session = tmp_path / "sessions" / "s.json"
    _sidecar(session, kind="daemon", endpoint=ENDPOINT + "/")
    # The registry path may be spelled differently (a symlinked checkout, a
    # trailing component); resolve() on both sides is what matches.
    spelled = tmp_path / "." / "sub" / ".."
    fetch = _Fetch(health={"ok": True}, workspaces=_workspaces(tmp_path / "other", spelled))
    decision = discover(tmp_path, "s", session, state_dir=_state_dir(tmp_path), fetch=fetch)
    assert decision.reason is None
    assert decision.target == RemoteTarget(
        endpoint=ENDPOINT, token="tok", workspace_id="ws1", session_id="s", workspace_root=tmp_path
    )
    assert decision.target.session_url == f"{ENDPOINT}/v1/workspaces/ws1/sessions/s"


def test_read_token_handles_missing_and_blank(tmp_path):
    assert read_token(tmp_path) is None
    (tmp_path / "token").write_text("  \n")
    assert read_token(tmp_path) is None
    (tmp_path / "token").write_text(" abc \n")
    assert read_token(tmp_path) == "abc"


def test_discover_reads_a_real_claim_sidecar(tmp_path):
    """The sidecar ``try_acquire`` writes is what ``discover`` reads — the
    same file the daemon leaves behind."""
    session = tmp_path / "sessions" / "s.json"
    session.parent.mkdir()
    session.write_text("{}")
    claim = try_acquire(session, kind="daemon", endpoint=ENDPOINT)
    assert claim is not None
    try:
        fetch = _Fetch(health={"ok": True}, workspaces=_workspaces(tmp_path))
        decision = discover(tmp_path, "s", session, state_dir=_state_dir(tmp_path), fetch=fetch)
    finally:
        claim.release()
    assert decision.target is not None and decision.target.workspace_id == "ws0"


class _Handler(BaseHTTPRequestHandler):
    seen: list[tuple[str, str | None]] = []

    def do_GET(self):  # noqa: N802 — stdlib naming
        type(self).seen.append((self.path, self.headers.get("Authorization")))
        if self.path == "/json":
            body = b'{"ok": true}'
        elif self.path == "/list":
            body = b"[1, 2]"
        elif self.path == "/bad":
            body = b"not json"
        else:
            self.send_response(404)
            self.end_headers()
            return
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):  # quiet
        pass


def test_urllib_fetch_speaks_bearer_http_and_fails_soft():
    server = HTTPServer(("127.0.0.1", 0), _Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    base = f"http://127.0.0.1:{server.server_address[1]}"
    try:
        assert urllib_fetch(f"{base}/json", "tok") == {"ok": True}
        assert urllib_fetch(f"{base}/json", None) == {"ok": True}
        assert urllib_fetch(f"{base}/list", "tok") is None  # a dict or nothing
        assert urllib_fetch(f"{base}/bad", "tok") is None
        assert urllib_fetch(f"{base}/missing", "tok") is None
    finally:
        server.shutdown()
        server.server_close()
    assert ("/json", "Bearer tok") in _Handler.seen
    assert ("/json", None) in _Handler.seen
    # A closed port: refused, not raised.
    assert urllib_fetch(base + "/json", "tok") is None
