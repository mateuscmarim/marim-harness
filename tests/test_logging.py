"""Tests for logging added to silent exception handlers."""

import logging

import pytest

from marim_harness.interfaces.cli import router
from tests.conftest import _make_deps

# --- _setup_logging -----------------------------------------------------------


def test_setup_logging_default_level(monkeypatch):
    monkeypatch.delenv("MARIM_DEBUG", raising=False)
    root = logging.getLogger()
    before = root.level
    try:
        router._setup_logging()
        assert root.level == logging.WARNING
    finally:
        root.level = before


def test_setup_logging_debug_level(monkeypatch):
    monkeypatch.setenv("MARIM_DEBUG", "1")
    root = logging.getLogger()
    before = root.level
    before_handlers = list(root.handlers)
    try:
        router._setup_logging()
        assert root.level == logging.DEBUG
    finally:
        root.level = before
        root.handlers = before_handlers


# --- route_logging_to_file ----------------------------------------------------


def test_route_logging_to_file_swaps_stderr_for_file(monkeypatch, tmp_path):
    """The TUI redirect replaces the stderr StreamHandler with a FileHandler so
    WARNING+ records land in a file instead of painting over the live screen."""
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    root = logging.getLogger()
    before_handlers = list(root.handlers)
    before_level = root.level
    try:
        router._setup_logging()
        assert [type(h).__name__ for h in root.handlers] == ["StreamHandler"]

        path = router.route_logging_to_file()

        assert path == tmp_path / "marim" / "marim.log"
        assert [type(h).__name__ for h in root.handlers] == ["FileHandler"]

        logging.getLogger("httpx").warning("Client error '400 Bad Request'")
        for h in root.handlers:
            h.flush()
        assert "Client error '400 Bad Request'" in path.read_text()
    finally:
        for h in root.handlers[:]:
            root.removeHandler(h)
            h.close()
        for h in before_handlers:
            root.addHandler(h)
        root.level = before_level


def test_route_logging_to_file_returns_none_on_oserror(monkeypatch):
    """A log file that can't be opened leaves logging untouched rather than
    crashing the launch."""
    root = logging.getLogger()
    before_handlers = list(root.handlers)
    try:
        router._setup_logging()

        def _boom(*args, **kwargs):
            raise OSError("read-only filesystem")

        monkeypatch.setattr(router.logging, "FileHandler", _boom)
        assert router.route_logging_to_file() is None
        # The original stderr handler survives — logging still works.
        assert [type(h).__name__ for h in root.handlers] == ["StreamHandler"]
    finally:
        root.handlers = before_handlers


# --- Tier 1: compaction summarizer failure ------------------------------------


@pytest.mark.anyio
async def test_compaction_logs_on_summarizer_failure(caplog):
    from pydantic_ai.messages import (
        ModelRequest,
        ModelResponse,
        TextPart,
        ToolCallPart,
        ToolReturnPart,
        UserPromptPart,
    )

    from marim_harness.compaction import compact_history_with_summary

    def _round(n: int) -> list:
        tid = f"t{n}"
        return [
            ModelRequest(parts=[UserPromptPart(content=f"prompt {n}")]),
            ModelResponse(
                parts=[
                    TextPart(content=f"thinking {n}"),
                    ToolCallPart(
                        tool_name="read_file",
                        args={"path": f"file{n}.py"},
                        tool_call_id=tid,
                    ),
                ]
            ),
            ModelRequest(
                parts=[
                    ToolReturnPart(
                        tool_name="read_file",
                        content=f"contents {n}",
                        tool_call_id=tid,
                    )
                ]
            ),
            ModelResponse(parts=[TextPart(content=f"answer {n}")]),
        ]

    history = []
    for n in range(20):
        history.extend(_round(n))

    async def boom(messages):
        raise RuntimeError("summary model down")

    with caplog.at_level(logging.WARNING, logger="marim_harness.compaction"):
        result, did = await compact_history_with_summary(
            history, max_tokens=1, summarizer=boom, keep_last_messages=8
        )

    assert did is True
    assert any("summarizer failed" in r.message for r in caplog.records)
    assert any(r.levelno == logging.WARNING for r in caplog.records)


# --- Tier 1: session autoname/rename titler failure --------------------------


@pytest.mark.anyio
async def test_autoname_logs_on_titler_failure(caplog, tmp_path):
    from marim_harness.runtime.deps import Deps
    from marim_harness.runtime.permissions import Mode
    from marim_harness.session.ctrl import SessionController
    from marim_harness.session.store import SessionManager

    manager = SessionManager(tmp_path)
    store = manager.create("test")
    deps = _make_deps(tmp_path)

    async def boom(history):
        raise RuntimeError("titler broken")

    ctrl = SessionController(
        store=store,
        manager=manager,
        deps=deps,
        max_context_tokens=100_000,
        keep_last_messages=20,
        titler=boom,
    )
    ctrl.history = ["some history"]
    store.auto_named = True

    with caplog.at_level(logging.WARNING, logger="marim_harness.session.ctrl"):
        await ctrl.maybe_autoname()

    assert any("autoname titler failed" in r.message for r in caplog.records)


@pytest.mark.anyio
async def test_rename_logs_on_titler_failure(caplog, tmp_path):
    from marim_harness.runtime.deps import Deps
    from marim_harness.session.ctrl import SessionController
    from marim_harness.session.store import SessionManager

    manager = SessionManager(tmp_path)
    store = manager.create("test")
    deps = _make_deps(tmp_path)

    async def boom(history):
        raise RuntimeError("titler broken")

    ctrl = SessionController(
        store=store,
        manager=manager,
        deps=deps,
        max_context_tokens=100_000,
        keep_last_messages=20,
        titler=boom,
    )
    ctrl.history = ["some history"]

    with caplog.at_level(logging.WARNING, logger="marim_harness.session.ctrl"):
        result = await ctrl.rename()

    assert result is None
    assert any("rename titler failed" in r.message for r in caplog.records)


# --- Tier 1: hook runner failure ---------------------------------------------


@pytest.mark.anyio
async def test_hook_runner_logs_on_command_failure(caplog, tmp_path, monkeypatch):
    from marim_harness.hooks import runner as hook_mod
    from marim_harness.hooks.runner import HookRunner

    hooks_cfg = {
        "SessionStart": [
            {
                "hooks": [
                    {"type": "command", "command": "echo ok"}
                ]
            }
        ]
    }
    runner = HookRunner(hooks_cfg)

    # Monkeypatch _run_one to raise, so the belt-and-suspenders handler fires
    async def _boom(command, payload, timeout):
        raise RuntimeError("unexpected hook failure")

    monkeypatch.setattr(hook_mod, "_run_one", _boom)

    payload = {"event": "SessionStart", "session_id": "s1", "cwd": str(tmp_path)}

    with caplog.at_level(logging.WARNING, logger="marim_harness.hooks.runner"):
        ctx = await runner.dispatch("SessionStart", payload)

    assert ctx is None  # hook failed, no context injected
    assert any("hook" in r.message and "failed" in r.message for r in caplog.records)


# --- Tier 1: catalog fetch failures ------------------------------------------


@pytest.mark.anyio
async def test_google_catalog_logs_on_failure(caplog, monkeypatch):
    from marim_harness.workspace.catalog import fetch_google_models

    # Force a failure by using a broken URL
    monkeypatch.setattr(
        "marim_harness.workspace.catalog._GOOGLE_MODELS_URL",
        "http://localhost:1/bad",
    )

    with caplog.at_level(logging.WARNING, logger="marim_harness.workspace.catalog"):
        result = await fetch_google_models(api_key="fake")

    assert result == []
    assert any("Google model catalog" in r.message for r in caplog.records)


@pytest.mark.anyio
async def test_openrouter_catalog_logs_on_failure(caplog, monkeypatch):
    from marim_harness.workspace.catalog import fetch_openrouter_models

    monkeypatch.setattr(
        "marim_harness.workspace.catalog._OPENROUTER_MODELS_URL",
        "http://localhost:1/bad",
    )

    with caplog.at_level(logging.WARNING, logger="marim_harness.workspace.catalog"):
        result = await fetch_openrouter_models(api_key="fake")

    assert result == []
    assert any("OpenRouter model catalog" in r.message for r in caplog.records)
