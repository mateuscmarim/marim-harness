import subprocess
import sys


def test_provider_imports_standalone():
    """Importing marim_harness.tools.provider as the very first module must not
    hit a circular import. Run in a fresh interpreter so module caching from
    other tests can't mask an import-order cycle."""
    result = subprocess.run(
        [sys.executable, "-c", "import marim_harness.tools.provider"],
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr
