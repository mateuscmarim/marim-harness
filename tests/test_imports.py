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


def test_status_imports_standalone():
    """Guards the tools<->status circular import: status must import cleanly when
    it is the FIRST tui module loaded in a process (the `marim sessions` path).
    An in-process test cannot catch this — by then `widgets` is already cached."""
    result = subprocess.run(
        [sys.executable, "-c", "import marim_harness.interfaces.tui.status"],
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr
