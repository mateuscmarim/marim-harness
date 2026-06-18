import os

import pytest

from marim_harness.config import global_config_path, save_env_settings


@pytest.fixture
def isolated_env():
    """Snapshot/restore os.environ so save_env_settings' os.environ writes don't
    leak across tests."""
    snapshot = dict(os.environ)
    yield
    os.environ.clear()
    os.environ.update(snapshot)


def test_creates_file_and_parent_dir(tmp_path):
    target = tmp_path / "nested" / ".env"
    returned = save_env_settings({"MARIM_LSP": "0"}, path=target)
    assert returned == target
    assert target.exists()
    assert "MARIM_LSP=0" in target.read_text()


def test_preserves_comments_and_other_keys(tmp_path):
    target = tmp_path / ".env"
    target.write_text("# my config\nOPENROUTER_API_KEY=sk-keep\nMARIM_LSP=1\n")
    save_env_settings({"MARIM_LSP": "0", "MARIM_PROACTIVE_MEMORY": "1"}, path=target)
    text = target.read_text()
    assert "# my config" in text  # comment survives
    assert "OPENROUTER_API_KEY=sk-keep" in text  # unmanaged key survives
    assert "MARIM_LSP=0" in text  # managed key updated in place
    assert "MARIM_LSP=1" not in text  # old value gone
    assert "MARIM_PROACTIVE_MEMORY=1" in text  # new managed key appended


def test_values_unquoted(tmp_path):
    target = tmp_path / ".env"
    save_env_settings({"MARIM_MAX_CONTEXT_TOKENS": "120000"}, path=target)
    assert "MARIM_MAX_CONTEXT_TOKENS=120000" in target.read_text()
    assert "'120000'" not in target.read_text()
    assert '"120000"' not in target.read_text()


def test_mirrors_into_os_environ(isolated_env, tmp_path):
    save_env_settings({"MARIM_LSP": "0"}, path=tmp_path / ".env")
    assert os.environ["MARIM_LSP"] == "0"


def test_defaults_to_global_config_path(isolated_env, monkeypatch, tmp_path):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    returned = save_env_settings({"MARIM_LSP": "0"})
    assert returned == global_config_path()
    assert returned == tmp_path / "marim" / ".env"
    assert returned.exists()
