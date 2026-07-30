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


def test_status_bar_imports_standalone():
    """Guards the tools<->status-chrome circular import: the status bar must import
    cleanly when it is the FIRST tui module loaded in a process (the
    `marim sessions` path). An in-process test cannot catch this — by then
    `widgets` is already cached.

    Was pointed at the retired ``tui.status`` module (which held StatusPresenter);
    ``widgets.status_bar`` is its successor and reaches the same way — up into
    ``compaction``/``usage`` and across into the ``format`` leaf — so it is the
    module that can still re-form the cycle."""
    result = subprocess.run(
        [sys.executable, "-c", "import marim_harness.interfaces.tui.widgets.status_bar"],
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr
