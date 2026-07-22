import sys
from pathlib import Path

import pytest

from marim_harness.lsp.generic import GenericStdioServer
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


@pytest.mark.anyio
async def test_generic_definition_round_trip(tmp_path):
    (tmp_path / "x.fake").write_text("hello\n")
    srv = GenericStdioServer.from_provider(_fake_provider(), tmp_path)
    async with srv.start_server():
        locs = await srv.request_definition("x.fake", 0, 0)
    assert locs and locs[0]["range"]["start"]["line"] == 0
