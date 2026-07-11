import os
import stat
import sys

import pytest
from dotenv import dotenv_values

from marim_harness.config import global_config_path, save_env_settings


@pytest.fixture
def isolated_env():
    """Snapshot/restore os.environ so save_env_settings' os.environ writes don't
    leak across tests."""
    snapshot = dict(os.environ)
    yield
    os.environ.clear()
    os.environ.update(snapshot)


def test_creates_file_and_parent_dir(isolated_env, tmp_path):
    target = tmp_path / "nested" / ".env"
    returned = save_env_settings({"MARIM_LSP": "0"}, path=target)
    assert returned == target
    assert target.exists()
    assert "MARIM_LSP=0" in target.read_text()


def test_preserves_comments_and_other_keys(isolated_env, tmp_path):
    target = tmp_path / ".env"
    target.write_text("# my config\nOPENROUTER_API_KEY=sk-keep\nMARIM_LSP=1\n")
    save_env_settings({"MARIM_LSP": "0", "MARIM_PROACTIVE_MEMORY": "1"}, path=target)
    text = target.read_text()
    assert "# my config" in text  # comment survives
    assert "OPENROUTER_API_KEY=sk-keep" in text  # unmanaged key survives
    assert "MARIM_LSP=0" in text  # managed key updated in place
    assert "MARIM_LSP=1" not in text  # old value gone
    assert "MARIM_PROACTIVE_MEMORY=1" in text  # new managed key appended


def test_values_unquoted(isolated_env, tmp_path):
    target = tmp_path / ".env"
    save_env_settings({"MARIM_MAX_CONTEXT_TOKENS": "120000"}, path=target)
    assert "MARIM_MAX_CONTEXT_TOKENS=120000" in target.read_text()
    assert "'120000'" not in target.read_text()
    assert '"120000"' not in target.read_text()


def test_save_env_settings_drops_retired_keys(isolated_env, tmp_path, monkeypatch):
    """``drop`` removes a retired key's line and its os.environ mirror in the
    same atomic save — used to replace MARIM_MAX_CONTEXT_TOKENS with
    MARIM_CONTEXT_BUDGET without leaving the deprecated line behind."""
    target = tmp_path / ".env"
    target.write_text("MARIM_MAX_CONTEXT_TOKENS=120000\nMARIM_MODEL=keep\n")
    monkeypatch.setenv("MARIM_MAX_CONTEXT_TOKENS", "120000")
    save_env_settings(
        {"MARIM_CONTEXT_BUDGET": "90000"},
        path=target,
        drop=("MARIM_MAX_CONTEXT_TOKENS",),
    )
    text = target.read_text()
    assert "MARIM_CONTEXT_BUDGET=90000" in text
    assert "MARIM_MAX_CONTEXT_TOKENS" not in text
    assert "MARIM_MODEL=keep" in text
    assert "MARIM_MAX_CONTEXT_TOKENS" not in os.environ


def test_mirrors_into_os_environ(isolated_env, tmp_path):
    save_env_settings({"MARIM_LSP": "0"}, path=tmp_path / ".env")
    assert os.environ["MARIM_LSP"] == "0"


def test_defaults_to_global_config_path(isolated_env, monkeypatch, tmp_path):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    returned = save_env_settings({"MARIM_LSP": "0"})
    assert returned == global_config_path()
    assert returned == tmp_path / "marim" / ".env"
    assert returned.exists()


def test_tui_path_round_trips_value_with_space_and_hash(isolated_env, tmp_path):
    """The TUI writer must quote a value containing whitespace and '#' so it
    survives write→reload. An unquoted write would let dotenv strip everything
    from the '#' on (an inline comment) and trim trailing space."""
    target = tmp_path / ".env"
    value = "http://proxy/v1 # staging"
    save_env_settings({"MARIM_BASE_URL": value}, path=target)
    assert dotenv_values(target)["MARIM_BASE_URL"] == value
    # a later write of a sibling key preserves the special-char value intact
    save_env_settings({"MARIM_MODEL": "openai/gpt-5.2"}, path=target)
    vals = dotenv_values(target)
    assert vals["MARIM_BASE_URL"] == value
    assert vals["MARIM_MODEL"] == "openai/gpt-5.2"


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX file mode bits not meaningful")
def test_env_file_is_owner_only(isolated_env, tmp_path):
    """The .env may hold OPENROUTER_API_KEY, so it must not be world-readable.
    After a write the file mode is 0600 (owner read/write only)."""
    target = tmp_path / ".env"
    save_env_settings({"OPENROUTER_API_KEY": "sk-secret"}, path=target)
    mode = stat.S_IMODE(target.stat().st_mode)
    assert mode == 0o600, oct(mode)


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX file mode bits not meaningful")
def test_existing_env_file_is_secured_on_update(isolated_env, tmp_path):
    """An already-world-readable .env is tightened to 0600 on the next write."""
    target = tmp_path / ".env"
    target.write_text("OPENROUTER_API_KEY=old\n")
    os.chmod(target, 0o644)
    save_env_settings({"OPENROUTER_API_KEY": "sk-new"}, path=target)
    assert stat.S_IMODE(target.stat().st_mode) == 0o600


def test_write_env_values_routes_through_atomic_write_text(isolated_env, tmp_path, monkeypatch):
    """The old writer hand-rolled its own mkstemp/replace dance with a
    DETERMINISTIC temp name (`target.name + ".tmp"`) and no fsync — exactly the
    anti-pattern atomic_io's module docstring calls out (two writers racing on
    the same target clobber each other's temp). Assert the save is delegated to
    atomic_write_text (unique temp per write, fsynced) rather than reimplemented
    locally."""
    import marim_harness.config.persist as persist_module

    calls = []
    original = persist_module.atomic_write_text

    def spy(path, text, **kwargs):
        calls.append((path, text))
        return original(path, text, **kwargs)

    monkeypatch.setattr(persist_module, "atomic_write_text", spy)
    target = tmp_path / ".env"
    save_env_settings({"MARIM_LSP": "0"}, path=target)
    assert len(calls) == 1
    assert calls[0][0] == target
    assert "MARIM_LSP=0" in calls[0][1]


def test_no_deterministic_tmp_sibling_left_behind(isolated_env, tmp_path):
    """A save must not leave a `<name>.tmp` (or any other temp) sibling behind —
    the old hand-rolled writer used a deterministic `target.name + ".tmp"` temp
    name, which is exactly the anti-pattern atomic_io's unique-per-write temp
    naming exists to avoid (two writers racing on that fixed name clobber each
    other). Routing through atomic_write_text means the temp is cleaned up by
    os.replace and nothing named "<target>.tmp" (or matching atomic_io's own
    ".<name>.*.tmp" pattern) should remain."""
    target = tmp_path / ".env"
    save_env_settings({"MARIM_LSP": "0"}, path=target)
    siblings = [p.name for p in tmp_path.iterdir() if p != target]
    assert siblings == [], siblings
