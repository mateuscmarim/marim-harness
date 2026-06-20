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
