import shlex
import sys
from pathlib import Path

import pytest

from marim_harness.lsp.generic import GenericStdioServer, _scrub_sensitive_env
from marim_harness.lsp.provider import parse_lsp_providers


def _fake_provider():
    fake = Path(__file__).parent / "fake_lsp_server.py"
    block = {
        "language": "fake",
        "extensions": [".fake"],
        "command": f"{sys.executable} {fake}",
        "rootMarkers": ["fake.toml"],
    }
    (p,) = parse_lsp_providers(
        block, bundled=False, source="global", plugin_root=None, strict=True
    )
    return p


def test_launch_info_uses_command_and_args(tmp_path):
    srv = GenericStdioServer.from_provider(_fake_provider(), tmp_path)
    # ProcessLaunchInfo carries the composed command string.
    assert "fake_lsp_server.py" in srv._launch_cmd  # see impl note below
    assert srv.language_id == "fake"


def test_initialize_params_declare_nav_capabilities(tmp_path):
    # A conformant LSP server gates each feature response on the matching client
    # capability, so GenericStdioServer must advertise every feature marim's nav
    # tools consume — otherwise declarative servers silently return empty for
    # symbols/hover/definition/references (see generic.py's capabilities comment).
    srv = GenericStdioServer.from_provider(_fake_provider(), tmp_path)
    params = srv._get_initialize_params(str(tmp_path))
    text_doc = params["capabilities"]["textDocument"]
    for feature in ("hover", "definition", "references", "documentSymbol"):
        assert feature in text_doc, f"missing textDocument.{feature} capability"
    assert "symbol" in params["capabilities"]["workspace"]


def test_args_with_spaces_are_shlex_quoted(tmp_path):
    """A naive " ".join lets an arg containing a space be re-split by the shell into
    two args. shlex.join quotes each discrete arg so it survives as one token — the
    composed command must round-trip back to the original tokens via shlex.split."""
    block = {
        "language": "spacey",
        "extensions": [".sp"],
        "command": "mybin",
        "args": ["--flag=a b", "plain", "has two spaces"],
    }
    (provider,) = parse_lsp_providers(
        block, bundled=False, source="global", plugin_root=None, strict=True
    )
    srv = GenericStdioServer.from_provider(provider, tmp_path)
    # The command is a shell string (may carry inline args); args are quoted after it.
    assert shlex.split(srv._launch_cmd) == ["mybin", "--flag=a b", "plain", "has two spaces"]


def test_scrub_sensitive_env_drops_credentials_keeps_plain():
    """Credentials/tokens are stripped from the env handed to a declarative
    third-party LSP server; ordinary vars (PATH, HOME, lang settings) survive."""
    scrubbed = _scrub_sensitive_env(
        {
            "PATH": "/usr/bin",
            "HOME": "/home/u",
            "LANG": "en_US.UTF-8",
            "OPENROUTER_API_KEY": "sk-secret",
            "MARIM_API_KEY": "secret",
            "GITHUB_TOKEN": "ghp_x",
            "AWS_SECRET_ACCESS_KEY": "abc",
            "DB_PASSWORD": "hunter2",
            "MY_CREDENTIAL": "c",
        }
    )
    assert scrubbed == {"PATH": "/usr/bin", "HOME": "/home/u", "LANG": "en_US.UTF-8"}


@pytest.mark.anyio
async def test_generic_definition_round_trip(tmp_path):
    (tmp_path / "x.fake").write_text("hello\n")
    srv = GenericStdioServer.from_provider(_fake_provider(), tmp_path)
    async with srv.start_server():
        locs = await srv.request_definition("x.fake", 0, 0)
    assert locs and locs[0]["range"]["start"]["line"] == 0
