"""Backend snapshots remain observations and do not alter host command ownership."""

from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from pydantic_ai.usage import RunUsage

from marim_harness.interfaces.tui.commands import dispatch
from marim_harness.interfaces.tui.link import LocalLinkInfo
from marim_harness.interfaces.tui.widgets.status_bar import backend_telemetry_text
from marim_harness.server.client import RemoteInfo
from marim_harness.server.http import _live_session_fields
from marim_harness.server.schema import Event


def test_local_and_session_detail_use_current_process_inventory():
    model = SimpleNamespace(
        backend_inventory={"backend": "claude-cli", "tools": ["Read"]},
        backend_telemetry={"thinking_tokens": 123, "state": "requires_action"},
    )
    harness = SimpleNamespace(
        current_model=model,
        model_id="claude-cli:sonnet",
        session=SimpleNamespace(history=[], usage=RunUsage(input_tokens=10), compact_threshold=100),
    )
    info = LocalLinkInfo(harness)
    fields = _live_session_fields(SimpleNamespace(harness=harness))
    assert fields["backend_inventory"] == info.backend_inventory
    assert fields["backend_telemetry"] == info.backend_telemetry
    assert fields["usage"]["input_tokens"] == 10
    model.backend_inventory = {"backend": "claude-cli", "tools": ["Bash"]}
    assert info.backend_inventory["tools"] == ["Bash"]
    harness.current_model = SimpleNamespace()
    assert info.backend_inventory == {}
    assert _live_session_fields(SimpleNamespace(harness=harness))["backend_telemetry"] == {}


def test_remote_snapshot_replacement_does_not_change_mode_or_usage():
    info = RemoteInfo(
        workspace_root=Path("/ws"), session_id="s", mode="ask", usage=RunUsage(input_tokens=17)
    )
    info.apply_session({"backend_inventory": {"tools": ["old"]}})
    info.observe(
        Event(
            seq=1,
            type="session.backend_state",
            ts="",
            data={
                "inventory": {"tools": ["new"]},
                "telemetry": {"state": "idle", "thinking_tokens": 900},
            },
        )
    )
    assert info.backend_inventory == {"tools": ["new"]}
    assert info.backend_telemetry == {"state": "idle", "thinking_tokens": 900}
    assert info.mode == "ask"
    assert info.usage.input_tokens == 17
    info.apply_session({})  # older daemon, additive fields optional
    assert info.backend_inventory == {"tools": ["new"]}
    info.apply_session({"backend_inventory": None, "backend_telemetry": []})
    assert info.backend_inventory == info.backend_telemetry == {}


def test_status_labels_thinking_estimate_separately():
    assert backend_telemetry_text({"thinking_tokens": 123, "state": "requires_action"}) == (
        "thinking ~123 · backend requires action"
    )
    assert backend_telemetry_text({"thinking_tokens": True, "state": "complete"}) == ""
    assert backend_telemetry_text({"thinking_tokens": -1}) == ""


@pytest.mark.anyio
async def test_inventory_does_not_shadow_builtins_or_claim_active_mcp():
    inventory = {
        "backend": "claude-cli",
        "tools": ["mcp__search"],
        "agents": ["explore"],
        "slash_commands": ["clear", "custom"],
        "permission_mode": "default",
        "mcp_servers": [{"name": "search", "status": "failed"}],
    }
    app = SimpleNamespace(
        link=SimpleNamespace(info=SimpleNamespace(backend_inventory=inventory)),
        post_system=AsyncMock(),
        reset_conversation=AsyncMock(),
    )
    await dispatch(app, "/clear")
    app.reset_conversation.assert_awaited_once()
    await dispatch(app, "/help")
    help_text = app.post_system.call_args.args[0]
    assert "Declared tools: `mcp__search`" in help_text
    assert "`/clear` — marim command takes precedence" in help_text
    assert "`/custom` — available" in help_text
    assert "Agents: `explore`" in help_text
    assert "Backend permission mode: `default`" in help_text
    await dispatch(app, "/mcp")
    assert "`search` — failed" in app.post_system.call_args.args[0]
    await dispatch(app, "/unknown")
    assert "Unknown command" in app.post_system.call_args.args[0]


def test_failed_optional_snapshot_is_payload_free(caplog):
    from marim_harness.config.backend_state import backend_snapshot

    class ClaudeAdapter:
        @property
        def backend_inventory(self):
            raise ValueError("PRIVATE")

    assert backend_snapshot(ClaudeAdapter(), "backend_inventory") == {}
    assert "ClaudeAdapter backend_inventory refresh failed" in caplog.text
    assert "PRIVATE" not in caplog.text


@pytest.mark.anyio
async def test_worktree_view_reads_backend_git_changes_without_checkpoints(tmp_path):
    import subprocess

    from marim_harness.interfaces.tui.commands import _worktree_list

    def git(*args):
        subprocess.run(["git", "-C", str(tmp_path), *args], check=True, capture_output=True)

    git("init", "-b", "before")
    git(
        "-c",
        "user.name=Test",
        "-c",
        "user.email=test@example.com",
        "commit",
        "--allow-empty",
        "-m",
        "init",
    )
    app = SimpleNamespace(post_system=AsyncMock())
    await _worktree_list(app, tmp_path, tmp_path)
    assert "`before`" in app.post_system.call_args.args[0]
    git("branch", "-m", "after")
    await _worktree_list(app, tmp_path, tmp_path)
    assert "`after`" in app.post_system.call_args.args[0]
    assert "`before`" not in app.post_system.call_args.args[0]
    refs = subprocess.run(
        ["git", "-C", str(tmp_path), "for-each-ref", "refs/marim"],
        check=True,
        capture_output=True,
        text=True,
    )
    assert refs.stdout == ""


@pytest.mark.anyio
async def test_declared_backend_command_forwards_exact_text_and_respects_queue():
    from unittest.mock import Mock

    inventory = {
        "backend": "claude-cli",
        "slash_commands": ["review", "terminal"],
        "terminal_slash_commands": ["terminal"],
    }
    app = SimpleNamespace(
        link=SimpleNamespace(info=SimpleNamespace(backend_inventory=inventory)),
        post_system=AsyncMock(),
        start_turn=AsyncMock(),
        turn_busy=False,
        compact_busy=False,
        queue=SimpleNamespace(paused=True, enqueue=Mock()),
    )
    await dispatch(app, "/review  preserve  spaces")
    app.start_turn.assert_awaited_once_with("/review  preserve  spaces")
    assert not app.queue.paused
    app.turn_busy = True
    await dispatch(app, "/review queued")
    app.queue.enqueue.assert_called_once_with("/review queued", None)
    app.compact_busy = True
    await dispatch(app, "/review blocked")
    assert "Compaction in progress" in app.post_system.call_args.args[0]
    await dispatch(app, "/terminal")
    assert "Unknown command" in app.post_system.call_args.args[0]
    inventory["backend"] = "unknown-cli"
    await dispatch(app, "/review")
    assert "Unknown command" in app.post_system.call_args.args[0]
    assert app.start_turn.await_count == app.queue.enqueue.call_count == 1


@pytest.mark.anyio
async def test_vcs_observation_refreshes_same_worktree_widget_and_preserves_ask(
    tmp_path, monkeypatch, caplog
):
    import subprocess

    from marim_harness.config.lifecycle import BackendObservation
    from marim_harness.interfaces.tui.widgets import AssistantMessage
    from tests.test_app import _app

    def git(*args):
        return subprocess.run(
            ["git", "-C", str(tmp_path), *args], check=True, capture_output=True, text=True
        )

    git("init", "-b", "before")
    git(
        "-c",
        "user.name=Test",
        "-c",
        "user.email=test@example.com",
        "commit",
        "--allow-empty",
        "-m",
        "init",
    )
    app = _app(tmp_path)
    async with app.run_test() as pilot:
        await pilot.pause()
        await dispatch(app, "/worktree")
        await pilot.pause()
        widgets = [w for w in app.query(AssistantMessage) if "`before`" in w.text]
        assert len(widgets) == 1
        widget = widgets[0]
        ask = app.host._park("approval", {"tool": "bash"})
        before_asks = app.host.pending_asks()
        before_status = app.host.status
        before_checkpoints = list(app.harness.checkpoints.list())
        git("branch", "-m", "after")
        await app.host._on_cli_activity(
            [
                BackendObservation(
                    telemetry={"vcs_revision": 1, "state": "idle", "cwd": "/untrusted/event/path"}
                )
            ]
        )
        await pilot.pause()
        assert "`after`" in widget.text and "`before`" not in widget.text
        assert widget.is_mounted
        assert len([w for w in app.query(AssistantMessage) if "| branch |" in w.text]) == 1
        assert app.host.pending_asks() == before_asks and not ask.future.done()
        assert app.host.status == before_status
        assert app.harness.checkpoints.list() == before_checkpoints
        assert git("for-each-ref", "refs/marim").stdout == ""
        git("branch", "-m", "latest")
        await app.host._on_cli_activity([BackendObservation(telemetry={"vcs_revision": 1})])
        await pilot.pause()
        assert "`after`" in widget.text  # replayed observation does not re-read git
        await app.host._on_cli_activity([BackendObservation(telemetry={"vcs_revision": 2})])
        await pilot.pause()
        assert "`latest`" in widget.text

        def fail(*args, **kwargs):
            raise RuntimeError("PRIVATE git failure")

        monkeypatch.setattr("marim_harness.workspace.worktree.list_worktrees", fail)
        await app.host._on_cli_activity([BackendObservation(telemetry={"vcs_revision": 3})])
        await pilot.pause()
        assert "worktree view refresh failed" in caplog.text
        assert "PRIVATE" not in caplog.text
        assert app.host.pending_asks() == before_asks and not ask.future.done()
        assert "`latest`" in widget.text
        await widget.remove()
        caplog.clear()
        await app.host._on_cli_activity([BackendObservation(telemetry={"vcs_revision": 4})])
        await pilot.pause()
        assert "worktree view refresh failed" not in caplog.text
        app.host.answer_ask(ask.id, {"approve": False})
