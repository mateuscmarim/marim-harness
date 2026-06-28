import json
from pathlib import Path

from pydantic_ai import Agent
from pydantic_ai.models.test import TestModel
from pydantic_ai.usage import RunUsage

from marim_harness.runtime import bootstrap
from marim_harness.runtime.permissions import Mode
from marim_harness.session import SessionManager


def _stub_model_plumbing(monkeypatch):
    """Keep build_harness off the provider packages and any network.

    bootstrap no longer calls build_model directly — model construction now
    flows through MultiModelSource.build → ModelSource.build. Patching
    ModelSource.build intercepts every provider's model construction without
    touching provider-specific import paths."""
    from pydantic_ai.models.test import TestModel

    from marim_harness.config.model import ModelSource

    monkeypatch.setattr(ModelSource, "build", lambda self, mid: TestModel())
    monkeypatch.setattr(bootstrap, "make_summarizer", lambda model: None)
    monkeypatch.setattr(bootstrap, "make_titler", lambda model: None)


def _isolate_sessions(monkeypatch, tmp_path: Path) -> Path:
    """Point session storage at a tmp dir (build_harness builds its own
    SessionManager keyed on XDG_DATA_HOME) and return its base dir so the test
    can construct a matching manager to seed/inspect sessions."""
    base = tmp_path / "xdg-data"
    monkeypatch.setenv("XDG_DATA_HOME", str(base))
    return base / "marim-harness" / "sessions"


def _stub_model_source_build(monkeypatch):
    """ModelSource.build(model_id) is hit when a fresh session inherits a model;
    keep it off provider packages by returning a TestModel that reports the id."""
    from marim_harness.runtime import bootstrap as _b

    monkeypatch.setattr(
        _b.ModelSource, "build", lambda self, model_id: TestModel()
    )


def _history() -> list:
    return Agent(TestModel(), instructions="x").run_sync("hi").all_messages()


def test_build_harness_wires_mcp_servers(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg"))
    monkeypatch.setenv("MARIM_TRUST_PROJECT_HOOKS", "1")  # project mcp.json needs trust
    _stub_model_plumbing(monkeypatch)

    ws = tmp_path / "ws"
    cfg = ws / ".marim" / "mcp.json"
    cfg.parent.mkdir(parents=True)
    cfg.write_text(
        json.dumps({"mcpServers": {"files": {"command": "npx", "args": ["fs"]}}}),
        encoding="utf-8",
    )

    harness = bootstrap.build_harness(ws, mode=Mode.ask)
    assert [s.tool_prefix for s in harness.mcp.mcp_servers] == ["files"]


def test_build_harness_skips_untrusted_project_mcp_servers(tmp_path: Path, monkeypatch):
    # Without trust, a project's own mcp.json must not be wired — its stdio servers
    # would launch subprocesses on connect, so a cloned repo can't auto-run them.
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg"))
    monkeypatch.delenv("MARIM_TRUST_PROJECT_HOOKS", raising=False)
    _stub_model_plumbing(monkeypatch)

    ws = tmp_path / "ws"
    cfg = ws / ".marim" / "mcp.json"
    cfg.parent.mkdir(parents=True)
    cfg.write_text(
        json.dumps({"mcpServers": {"evil": {"command": "sh", "args": ["-c", "x"]}}}),
        encoding="utf-8",
    )

    harness = bootstrap.build_harness(ws, mode=Mode.ask)
    assert harness.mcp.mcp_servers == []  # untrusted project config dropped


def test_build_harness_seeds_config_disabled_servers(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg"))
    monkeypatch.setenv("MARIM_TRUST_PROJECT_HOOKS", "1")  # project mcp.json needs trust
    _stub_model_plumbing(monkeypatch)

    ws = tmp_path / "ws"
    cfg = ws / ".marim" / "mcp.json"
    cfg.parent.mkdir(parents=True)
    cfg.write_text(
        json.dumps(
            {
                "mcpServers": {
                    "on": {"command": "npx", "args": ["a"]},
                    "off": {"command": "npx", "args": ["b"], "enabled": False},
                }
            }
        ),
        encoding="utf-8",
    )

    harness = bootstrap.build_harness(ws, mode=Mode.ask)
    # Both servers are built (so "off" can be enabled in-session)...
    assert {s.tool_prefix for s in harness.mcp.mcp_servers} == {"on", "off"}
    # ...but the config-disabled one is seeded as disabled.
    assert harness.mcp.disabled == {"off"}


def test_build_harness_logs_malformed_mcp_spec(tmp_path: Path, monkeypatch, caplog):
    """A malformed MCP spec is dropped (so one bad entry can't sink the rest), but
    the user must get feedback — the warning is logged, not silently discarded."""
    import logging

    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg"))
    monkeypatch.setenv("MARIM_TRUST_PROJECT_HOOKS", "1")  # project mcp.json needs trust
    _stub_model_plumbing(monkeypatch)

    ws = tmp_path / "ws"
    cfg = ws / ".marim" / "mcp.json"
    cfg.parent.mkdir(parents=True)
    cfg.write_text(
        # "bad" is neither stdio (no command) nor HTTP/SSE (no url) -> skipped.
        json.dumps({"mcpServers": {"bad": {"nonsense": True}}}),
        encoding="utf-8",
    )

    with caplog.at_level(logging.WARNING, logger="marim_harness.runtime.bootstrap"):
        harness = bootstrap.build_harness(ws, mode=Mode.ask)

    assert harness.mcp.mcp_servers == []  # the bad spec was dropped
    assert any("bad" in r.getMessage() for r in caplog.records)


def test_build_harness_wires_command_policy(tmp_path: Path, monkeypatch):
    """The configured command denylist reaches deps.command_policy, so the bash
    tool enforces it in every mode."""
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg"))
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-test")
    monkeypatch.setenv("MARIM_COMMAND_DENYLIST", "rm -rf")
    _stub_model_plumbing(monkeypatch)

    harness = bootstrap.build_harness(tmp_path / "ws", mode=Mode.auto)
    assert harness.deps.workspace.command_policy.check("rm -rf /") is not None
    assert harness.deps.workspace.command_policy.check("ls") is None


def test_build_harness_no_mcp_config_is_empty(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg"))
    _stub_model_plumbing(monkeypatch)
    harness = bootstrap.build_harness(tmp_path / "ws", mode=Mode.ask)
    assert harness.mcp.mcp_servers == []


# --- model inheritance / resume (bootstrap.py lines ~34-43) ---


def test_fresh_harness_inherits_model_from_latest_session(tmp_path, monkeypatch):
    """A non-resume harness picks up the model id from the most recent saved
    session in this workspace when it differs from the config default."""
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg"))
    monkeypatch.delenv("MARIM_PROVIDER", raising=False)
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-test")
    monkeypatch.delenv("MARIM_MODEL", raising=False)
    _stub_model_plumbing(monkeypatch)
    _stub_model_source_build(monkeypatch)
    base = _isolate_sessions(monkeypatch, tmp_path)

    ws = tmp_path / "ws"
    # Seed a saved session for this workspace carrying a non-default model.
    seeded = SessionManager(ws, base_dir=base).create("Earlier")
    seeded.model = "openai/gpt-5.2"
    seeded.save(_history(), RunUsage())
    assert seeded.model != bootstrap.load_config().model  # genuinely differs

    harness = bootstrap.build_harness(ws, mode=Mode.ask)

    assert harness.model_id == "openai/gpt-5.2"
    # MultiModelSource.label uses ':' as provider:model separator (was '/' with ModelSource).
    assert harness.model_label == "openrouter:openai/gpt-5.2"


def test_fresh_harness_falls_back_to_config_default(tmp_path, monkeypatch):
    """With no prior session, the harness uses the config default model."""
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg"))
    monkeypatch.delenv("MARIM_PROVIDER", raising=False)
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-test")
    monkeypatch.delenv("MARIM_MODEL", raising=False)
    _stub_model_plumbing(monkeypatch)
    _isolate_sessions(monkeypatch, tmp_path)

    default_model = bootstrap.load_config().model
    harness = bootstrap.build_harness(tmp_path / "ws", mode=Mode.ask)

    # model_id is now the qualified form "provider:bare_model" produced by MultiModelSource.
    assert harness.model_id == f"openrouter:{default_model}"
    assert harness.session.store.model is None  # brand-new session, nothing inherited


def test_build_harness_sets_hooks_when_global_config_present(tmp_path, monkeypatch):
    import json

    from marim_harness.runtime.bootstrap import build_harness
    from marim_harness.runtime.permissions import Mode

    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg"))
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "data"))
    monkeypatch.setenv("MARIM_API_KEY", "x")
    _stub_model_plumbing(monkeypatch)
    cfg_path = tmp_path / "xdg" / "marim" / "hooks.json"
    cfg_path.parent.mkdir(parents=True, exist_ok=True)
    cfg_path.write_text(json.dumps({"hooks": {"Stop": [{"hooks": []}]}}))

    harness = build_harness(tmp_path / "ws", mode=Mode.ask)
    assert harness.deps.hooks is not None


def test_build_harness_hooks_none_without_config(tmp_path, monkeypatch):
    from marim_harness.runtime.bootstrap import build_harness

    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg"))
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "data"))
    monkeypatch.setenv("MARIM_API_KEY", "x")
    _stub_model_plumbing(monkeypatch)

    harness = build_harness(tmp_path / "ws", mode=Mode.ask)
    assert harness.deps.hooks is None


def test_build_harness_uses_multi_model_source(monkeypatch, tmp_path):
    from pydantic_ai.models.test import TestModel

    import marim_harness.runtime.bootstrap as b
    from marim_harness.config.model import MultiModelSource

    monkeypatch.setenv("OPENROUTER_API_KEY", "or-key")
    monkeypatch.setenv("MARIM_BASE_URL", "http://localhost:1234/v1")
    # Avoid constructing real provider models or aux agents in the test:
    monkeypatch.setattr(MultiModelSource, "build", lambda self, mid: TestModel())
    monkeypatch.setattr(b, "make_summarizer", lambda model: None)
    monkeypatch.setattr(b, "make_titler", lambda model: None)
    h = b.build_harness(tmp_path, mode=Mode.ask)
    assert isinstance(h.model_source, MultiModelSource)
    assert set(h.model_source.sources) >= {"openrouter", "local"}
    assert h.model_id.startswith("openrouter:")  # qualified default


def test_resume_reattaches_to_latest_and_replays_history(tmp_path, monkeypatch):
    """resume=True reattaches to the most recent session and replays its saved
    history (message count > 0)."""
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg"))
    monkeypatch.delenv("MARIM_PROVIDER", raising=False)
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-test")
    monkeypatch.delenv("MARIM_MODEL", raising=False)
    _stub_model_plumbing(monkeypatch)
    _stub_model_source_build(monkeypatch)
    base = _isolate_sessions(monkeypatch, tmp_path)

    ws = tmp_path / "ws"
    history = _history()
    seeded = SessionManager(ws, base_dir=base).create("Resume Me")
    seeded.model = "openai/gpt-5.2"
    seeded.save(history, RunUsage())

    harness = bootstrap.build_harness(ws, mode=Mode.ask, resume=True)

    # Reattached to the saved session, not a fresh one.
    assert harness.session.store.session_id == seeded.session_id
    # History was replayed.
    assert len(harness.session.history) == len(history) > 0
    # The saved model was applied on resume.
    assert harness.model_id == "openai/gpt-5.2"


def test_build_harness_uses_configured_default_mode(monkeypatch, tmp_path):
    """No explicit mode -> the interactive default from MARIM_DEFAULT_MODE."""
    _stub_model_plumbing(monkeypatch)
    _isolate_sessions(monkeypatch, tmp_path)
    monkeypatch.setenv("MARIM_DEFAULT_MODE", "auto")
    harness = bootstrap.build_harness(tmp_path / "ws")
    assert harness.deps.workspace.mode is Mode.auto


def test_build_harness_defaults_to_ask_without_config(monkeypatch, tmp_path):
    _stub_model_plumbing(monkeypatch)
    _isolate_sessions(monkeypatch, tmp_path)
    monkeypatch.delenv("MARIM_DEFAULT_MODE", raising=False)
    harness = bootstrap.build_harness(tmp_path / "ws")
    assert harness.deps.workspace.mode is Mode.ask


def test_build_harness_explicit_mode_overrides_config_default(monkeypatch, tmp_path):
    _stub_model_plumbing(monkeypatch)
    _isolate_sessions(monkeypatch, tmp_path)
    monkeypatch.setenv("MARIM_DEFAULT_MODE", "auto")
    harness = bootstrap.build_harness(tmp_path / "ws", mode=Mode.plan)
    assert harness.deps.workspace.mode is Mode.plan
