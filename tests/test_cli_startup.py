import subprocess
import sys


def _imports_pydantic_ai(module: str) -> bool:
    """True if importing `module` in a FRESH interpreter pulls in pydantic_ai.

    Must be a subprocess: sys.modules is process-global, and the rest of the test
    suite imports pydantic_ai, so an in-process check would always see it loaded.
    """
    code = (
        f"import {module}\n"
        "import sys\n"
        "raise SystemExit(1 if 'pydantic_ai' in sys.modules else 0)"
    )
    return subprocess.run([sys.executable, "-c", code]).returncode == 1


def test_router_import_does_not_load_pydantic_ai():
    # Importing the CLI router must not drag in pydantic_ai, or every command
    # (config/models/--help) pays ~1s for an agent it never builds.
    assert not _imports_pydantic_ai("marim_harness.interfaces.cli.router")


def test_default_cmd_import_does_not_load_pydantic_ai():
    # The default command's module must stay import-clean so `marim --help` and
    # arg-validation errors exit before pydantic_ai loads. The real TUI/headless
    # launch still imports it inside run_default() — that's expected and untested
    # here.
    assert not _imports_pydantic_ai("marim_harness.interfaces.cli.default_cmd")


def test_trust_cmd_import_does_not_load_pydantic_ai():
    # `marim trust` is a cheap status command (reads .marim/mcp.json /
    # hooks.json, no agent). Its import chain (trust_cmd -> trust_surface ->
    # mcp.config) must not drag in pydantic_ai (MCPToolset construction) or
    # fastmcp (the MCP transports) — both are only needed by the code paths
    # that actually build/connect an MCP server, never by a status readout.
    assert not _imports_pydantic_ai("marim_harness.interfaces.cli.trust_cmd")


def test_trust_cmd_import_does_not_load_fastmcp():
    code = (
        "import marim_harness.interfaces.cli.trust_cmd\n"
        "import sys\n"
        "raise SystemExit(1 if 'fastmcp' in sys.modules else 0)"
    )
    result = subprocess.run([sys.executable, "-c", code])
    assert result.returncode == 0


def test_help_exits_fast_without_pydantic_ai(tmp_path):
    # `marim --help` must print usage and exit 0 in a fresh interpreter without
    # ever importing pydantic_ai.
    code = (
        "import sys\n"
        "sys.argv = ['marim', '--help']\n"
        "from marim_harness.interfaces.cli.router import main\n"
        "try:\n"
        "    main()\n"
        "except SystemExit as e:\n"
        "    assert e.code in (0, None), e.code\n"
        "assert 'pydantic_ai' not in sys.modules, 'pydantic_ai loaded on --help'\n"
    )
    result = subprocess.run(
        [sys.executable, "-c", code], capture_output=True, text=True, cwd=tmp_path
    )
    assert result.returncode == 0, result.stderr
