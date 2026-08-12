# `marim update` Command — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a `marim update` CLI command that upgrades the installed `marim-harness` package to the latest version, with a `--check` flag for version-only checks.

**Architecture:** A new `update_cmd.py` module following the existing CLI pattern (`import_cmd.py`, `trust_cmd.py`), registered in `router.py`. Two helpers: `_check_latest()` hits the PyPI JSON API via `httpx`, `_do_upgrade()` shells out to `uv tool upgrade` with a `pip install` fallback. `main()` ties them together via argparse.

**Tech Stack:** Python stdlib (`argparse`, `subprocess`, `importlib.metadata`), `httpx` (already a dependency).

## Global Constraints

- Python >= 3.10 (no 3.11+ only syntax)
- Ruff line length 100, max complexity 10
- No new dependencies — `httpx` and `importlib.metadata` are already in the tree
- Follow existing CLI module pattern: `main(argv, *, out, err)` returning int
- Tests use `unittest.mock`, `tmp_path`, `capsys`
- All tests must pass before committing

---

### Task 1: Module skeleton and router registration

**Files:**
- Create: `src/marim_harness/interfaces/cli/update_cmd.py`
- Modify: `src/marim_harness/interfaces/cli/router.py` (line 13: `_MANAGEMENT` set)

**Interfaces:**
- Produces: `update_cmd.main(argv: list[str], *, out=None, err=None) -> int`

- [ ] **Step 1: Create `update_cmd.py` with a stub `main()` function**

```python
"""`marim update` — upgrade the installed marim-harness package."""

import argparse
import sys


def main(argv: list[str], *, out=None, err=None) -> int:
    out = sys.stdout if out is None else out
    err = sys.stderr if err is None else err

    parser = argparse.ArgumentParser(
        prog="marim update",
        description="Upgrade marim-harness to the latest version.",
    )
    parser.add_argument(
        "--check",
        action="store_true",
        help="Only report whether a newer version is available; do not install.",
    )
    args = parser.parse_args(argv)

    print("update: not yet implemented", file=out)
    return 0
```

- [ ] **Step 2: Register `"update"` in `router.py`'s `_MANAGEMENT` set**

In `src/marim_harness/interfaces/cli/router.py`, line 13, add `"update"` to the set:

```python
_MANAGEMENT = {"sessions", "config", "models", "plugin", "mcp", "serve", "trust", "import", "update"}
```

- [ ] **Step 3: Smoke test — run `marim update` from the command line**

Run: `uv run marim update`
Expected: prints "update: not yet implemented", exits 0.

- [ ] **Step 4: Commit**

```bash
git add src/marim_harness/interfaces/cli/update_cmd.py src/marim_harness/interfaces/cli/router.py
git commit -m "feat(update): add update command skeleton and router registration"
```

---

### Task 2: Implement `_check_latest()`

**Files:**
- Modify: `src/marim_harness/interfaces/cli/update_cmd.py`
- Create: `tests/test_update_cmd.py`

**Interfaces:**
- Consumes: `update_cmd.main()` (from Task 1)
- Produces: `UpdateInfo` dataclass with fields `current: str`, `latest: str`, `is_outdated: bool`, `release_url: str`
- Produces: `_check_latest() -> UpdateInfo` (may raise `PackageNotFoundError`, `RuntimeError` on network failure)

- [ ] **Step 1: Write tests for `_check_latest()` — outdated case**

In `tests/test_update_cmd.py`:

```python
"""Tests for the marim update command."""

import json
import sys
from unittest.mock import patch

import pytest


def test_check_latest_outdated():
    from marim_harness.interfaces.cli.update_cmd import _check_latest, UpdateInfo

    mock_response = {
        "info": {
            "version": "9.9.9",
            "release_url": "https://pypi.org/project/marim-harness/9.9.9/",
        }
    }
    with patch("httpx.get") as mock_get:
        mock_get.return_value.json.return_value = mock_response
        mock_get.return_value.raise_for_status = lambda: None
        with patch("importlib.metadata.version", return_value="0.3.0"):
            result = _check_latest()

    assert result.current == "0.3.0"
    assert result.latest == "9.9.9"
    assert result.is_outdated is True
    assert "9.9.9" in result.release_url
```

- [ ] **Step 2: Write tests for `_check_latest()` — already current**

Append to `tests/test_update_cmd.py`:

```python
def test_check_latest_current():
    from marim_harness.interfaces.cli.update_cmd import _check_latest

    mock_response = {
        "info": {
            "version": "0.3.0",
            "release_url": "https://pypi.org/project/marim-harness/0.3.0/",
        }
    }
    with patch("httpx.get") as mock_get:
        mock_get.return_value.json.return_value = mock_response
        mock_get.return_value.raise_for_status = lambda: None
        with patch("importlib.metadata.version", return_value="0.3.0"):
            result = _check_latest()

    assert result.is_outdated is False
```

- [ ] **Step 3: Write tests for `_check_latest()` — network error**

Append to `tests/test_update_cmd.py`:

```python
def test_check_latest_network_error():
    from marim_harness.interfaces.cli.update_cmd import _check_latest

    with patch("httpx.get", side_effect=RuntimeError("connection refused")):
        with patch("importlib.metadata.version", return_value="0.3.0"):
            with pytest.raises(RuntimeError, match="Could not reach PyPI"):
                _check_latest()
```

- [ ] **Step 4: Write test for `_check_latest()` — package not installed**

Append to `tests/test_update_cmd.py`:

```python
def test_check_latest_not_installed():
    from importlib.metadata import PackageNotFoundError
    from marim_harness.interfaces.cli.update_cmd import _check_latest

    with patch("importlib.metadata.version", side_effect=PackageNotFoundError):
        with pytest.raises(PackageNotFoundError):
            _check_latest()
```

- [ ] **Step 5: Run tests to verify they fail**

Run: `uv run pytest tests/test_update_cmd.py -v`
Expected: all four tests FAIL (name errors, imports missing).

- [ ] **Step 6: Define the `UpdateInfo` dataclass and implement `_check_latest()`**

In `src/marim_harness/interfaces/cli/update_cmd.py`, add after the imports and before `main()`:

```python
from dataclasses import dataclass
from importlib.metadata import PackageNotFoundError, version

import httpx


@dataclass(frozen=True)
class UpdateInfo:
    current: str
    latest: str
    release_url: str

    @property
    def is_outdated(self) -> bool:
        return self.current != self.latest


def _check_latest() -> UpdateInfo:
    """Fetch the latest marim-harness version from PyPI and compare to installed."""
    current = version("marim-harness")
    try:
        resp = httpx.get(
            "https://pypi.org/pypi/marim-harness/json",
            timeout=httpx.Timeout(10.0),
            follow_redirects=True,
        )
        resp.raise_for_status()
        data = resp.json()
        latest = data["info"]["version"]
        url = data["info"].get("release_url", "")
    except httpx.HTTPError as exc:
        raise RuntimeError(f"Could not reach PyPI to check for updates: {exc}") from exc
    return UpdateInfo(current=current, latest=latest, release_url=url)
```

- [ ] **Step 7: Run tests to verify they pass**

Run: `uv run pytest tests/test_update_cmd.py -v`
Expected: all four tests PASS.

- [ ] **Step 8: Commit**

```bash
git add src/marim_harness/interfaces/cli/update_cmd.py tests/test_update_cmd.py
git commit -m "feat(update): implement _check_latest() with PyPI JSON API"
```

---

### Task 3: Implement `_do_upgrade()`

**Files:**
- Modify: `src/marim_harness/interfaces/cli/update_cmd.py`
- Modify: `tests/test_update_cmd.py`

**Interfaces:**
- Consumes: `UpdateInfo` (from Task 2)
- Produces: `_do_upgrade() -> int` (subprocess exit code)

- [ ] **Step 1: Write tests for `_do_upgrade()`**

Append to `tests/test_update_cmd.py`:

```python
def test_do_upgrade_uv_tool_succeeds():
    from marim_harness.interfaces.cli.update_cmd import _do_upgrade

    with patch("subprocess.run") as mock_run:
        mock_run.return_value.returncode = 0
        result = _do_upgrade()
        assert result == 0
        mock_run.assert_called_once()
        args = mock_run.call_args[0][0]
        assert args[0] == "uv"
        assert args[1] == "tool"
        assert args[2] == "upgrade"
        assert "marim-harness" in args


def test_do_upgrade_falls_back_to_pip():
    from marim_harness.interfaces.cli.update_cmd import _do_upgrade

    with patch("subprocess.run") as mock_run:
        # First call (uv tool) fails, second (pip) succeeds
        mock_run.return_value.returncode = 0
        mock_run.side_effect = [
            type("Result", (), {"returncode": 1})(),  # uv tool fails
            type("Result", (), {"returncode": 0})(),  # pip succeeds
        ]
        result = _do_upgrade()
        assert result == 0
        assert mock_run.call_count == 2
        first_args = mock_run.call_args_list[0][0][0]
        second_args = mock_run.call_args_list[1][0][0]
        assert first_args[0] == "uv"
        assert second_args[0] == "pip"


def test_do_upgrade_pip_fails():
    from marim_harness.interfaces.cli.update_cmd import _do_upgrade

    with patch("subprocess.run") as mock_run:
        mock_run.return_value.returncode = 1
        result = _do_upgrade()
        assert result == 1
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_update_cmd.py::test_do_upgrade_uv_tool_succeeds tests/test_update_cmd.py::test_do_upgrade_falls_back_to_pip tests/test_update_cmd.py::test_do_upgrade_pip_fails -v`
Expected: all three FAIL (name error).

- [ ] **Step 3: Implement `_do_upgrade()`**

In `src/marim_harness/interfaces/cli/update_cmd.py`, add after `_check_latest`:

```python
import subprocess


def _do_upgrade() -> int:
    """Upgrade marim-harness: try uv tool first, then pip as fallback."""
    try:
        result = subprocess.run(
            ["uv", "tool", "upgrade", "marim-harness"],
            check=False,
        )
    except FileNotFoundError:
        result = None
    if result is None or result.returncode != 0:
        result = subprocess.run(
            [sys.executable, "-m", "pip", "install", "--upgrade", "marim-harness"],
            check=False,
        )
    return result.returncode
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/test_update_cmd.py::test_do_upgrade_uv_tool_succeeds tests/test_update_cmd.py::test_do_upgrade_falls_back_to_pip tests/test_update_cmd.py::test_do_upgrade_pip_fails -v`
Expected: all three PASS.

- [ ] **Step 5: Commit**

```bash
git add src/marim_harness/interfaces/cli/update_cmd.py tests/test_update_cmd.py
git commit -m "feat(update): implement _do_upgrade() with uv tool / pip fallback"
```

---

### Task 4: Complete `main()` — `--check` path

**Files:**
- Modify: `src/marim_harness/interfaces/cli/update_cmd.py`
- Modify: `tests/test_update_cmd.py`

**Interfaces:**
- Consumes: `UpdateInfo`, `_check_latest()`, `_do_upgrade()`
- Produces: `main(argv, *, out, err) -> int` (complete implementation)

- [ ] **Step 1: Write tests for `main()` with `--check`**

Append to `tests/test_update_cmd.py`:

```python
from io import StringIO


def test_main_check_outdated():
    from marim_harness.interfaces.cli.update_cmd import main, UpdateInfo

    info = UpdateInfo(current="0.3.0", latest="9.9.9",
                      release_url="https://pypi.org/project/marim-harness/9.9.9/")
    with patch("marim_harness.interfaces.cli.update_cmd._check_latest", return_value=info):
        out = StringIO()
        err = StringIO()
        result = main(["--check"], out=out, err=err)
        output = out.getvalue()
        assert result == 0
        assert "0.3.0" in output
        assert "9.9.9" in output


def test_main_check_current():
    from marim_harness.interfaces.cli.update_cmd import main, UpdateInfo

    info = UpdateInfo(current="0.3.0", latest="0.3.0",
                      release_url="https://pypi.org/project/marim-harness/0.3.0/")
    with patch("marim_harness.interfaces.cli.update_cmd._check_latest", return_value=info):
        out = StringIO()
        err = StringIO()
        result = main(["--check"], out=out, err=err)
        output = out.getvalue()
        assert result == 0
        assert "already the latest" in output.lower()


def test_main_check_network_error():
    from marim_harness.interfaces.cli.update_cmd import main

    with patch("marim_harness.interfaces.cli.update_cmd._check_latest",
               side_effect=RuntimeError("Could not reach PyPI")):
        out = StringIO()
        err = StringIO()
        result = main(["--check"], out=out, err=err)
        assert result == 1
        assert "Could not reach PyPI" in err.getvalue()


def test_main_check_not_installed():
    from importlib.metadata import PackageNotFoundError
    from marim_harness.interfaces.cli.update_cmd import main

    with patch("marim_harness.interfaces.cli.update_cmd._check_latest",
               side_effect=PackageNotFoundError):
        out = StringIO()
        err = StringIO()
        result = main(["--check"], out=out, err=err)
        assert result == 1
        assert "not installed" in out.getvalue().lower()
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_update_cmd.py::test_main_check_outdated tests/test_update_cmd.py::test_main_check_current tests/test_update_cmd.py::test_main_check_network_error tests/test_update_cmd.py::test_main_check_not_installed -v`
Expected: FAIL (main still prints "not yet implemented").

- [ ] **Step 3: Replace the stub `main()` with the full implementation**

In `src/marim_harness/interfaces/cli/update_cmd.py`, replace the existing `main()` with:

```python
def main(argv: list[str], *, out=None, err=None) -> int:
    out = sys.stdout if out is None else out
    err = sys.stderr if err is None else err

    parser = argparse.ArgumentParser(
        prog="marim update",
        description="Upgrade marim-harness to the latest version.",
    )
    parser.add_argument(
        "--check",
        action="store_true",
        help="Only report whether a newer version is available; do not install.",
    )
    args = parser.parse_args(argv)

    if args.check:
        try:
            info = _check_latest()
        except PackageNotFoundError:
            print(
                "marim-harness is not installed as a package (running from source?).",
                file=out,
            )
            return 1
        except RuntimeError as exc:
            print(exc, file=err)
            return 1

        if info.is_outdated:
            print(
                f"marim-harness {info.current} is outdated — "
                f"{info.latest} is available.",
                file=out,
            )
            if info.release_url:
                print(info.release_url, file=out)
        else:
            print(
                f"marim-harness {info.current} is already the latest version.",
                file=out,
            )
        return 0

    # Plain `marim update` — check first, upgrade if needed.
    try:
        info = _check_latest()
    except PackageNotFoundError:
        print(
            "marim-harness is not installed as a package (running from source?).",
            file=out,
        )
        return 1
    except RuntimeError as exc:
        print(exc, file=err)
        return 1

    if not info.is_outdated:
        print(
            f"marim-harness {info.current} is already the latest version.",
            file=out,
        )
        return 0

    print(f"Upgrading marim-harness from {info.current} to {info.latest}...", file=out)
    code = _do_upgrade()
    if code == 0:
        print(f"Upgraded to marim-harness {info.latest}.", file=out)
    return code
```

- [ ] **Step 4: Run the `--check` tests to verify they pass**

Run: `uv run pytest tests/test_update_cmd.py::test_main_check_outdated tests/test_update_cmd.py::test_main_check_current tests/test_update_cmd.py::test_main_check_network_error tests/test_update_cmd.py::test_main_check_not_installed -v`
Expected: all four PASS.

- [ ] **Step 5: Commit**

```bash
git add src/marim_harness/interfaces/cli/update_cmd.py tests/test_update_cmd.py
git commit -m "feat(update): complete main() with --check path"
```

---

### Task 5: Tests for `main()` upgrade path and edge cases

**Files:**
- Modify: `tests/test_update_cmd.py`

- [ ] **Step 1: Write test for `main` with upgrade (uv tool succeeds)**

Append to `tests/test_update_cmd.py`:

```python
def test_main_upgrade_succeeds():
    from marim_harness.interfaces.cli.update_cmd import main, UpdateInfo

    info = UpdateInfo(current="0.3.0", latest="9.9.9",
                      release_url="https://pypi.org/project/marim-harness/9.9.9/")
    with patch("marim_harness.interfaces.cli.update_cmd._check_latest", return_value=info):
        with patch("marim_harness.interfaces.cli.update_cmd._do_upgrade", return_value=0):
            out = StringIO()
            err = StringIO()
            result = main([], out=out, err=err)
            output = out.getvalue()
            assert result == 0
            assert "Upgraded" in output


def test_main_upgrade_fails():
    from marim_harness.interfaces.cli.update_cmd import main, UpdateInfo

    info = UpdateInfo(current="0.3.0", latest="9.9.9",
                      release_url="https://pypi.org/project/marim-harness/9.9.9/")
    with patch("marim_harness.interfaces.cli.update_cmd._check_latest", return_value=info):
        with patch("marim_harness.interfaces.cli.update_cmd._do_upgrade", return_value=1):
            out = StringIO()
            err = StringIO()
            result = main([], out=out, err=err)
            assert result == 1


def test_main_upgrade_already_latest():
    from marim_harness.interfaces.cli.update_cmd import main, UpdateInfo

    info = UpdateInfo(current="0.3.0", latest="0.3.0",
                      release_url="https://pypi.org/project/marim-harness/0.3.0/")
    with patch("marim_harness.interfaces.cli.update_cmd._check_latest", return_value=info):
        with patch("marim_harness.interfaces.cli.update_cmd._do_upgrade") as mock_upgrade:
            out = StringIO()
            err = StringIO()
            result = main([], out=out, err=err)
            output = out.getvalue()
            assert result == 0
            assert "already the latest" in output.lower()
            mock_upgrade.assert_not_called()


def test_main_upgrade_not_installed():
    from importlib.metadata import PackageNotFoundError
    from marim_harness.interfaces.cli.update_cmd import main

    with patch("marim_harness.interfaces.cli.update_cmd._check_latest",
               side_effect=PackageNotFoundError):
        out = StringIO()
        err = StringIO()
        result = main([], out=out, err=err)
        assert result == 1
        assert "not installed" in out.getvalue().lower()
```

- [ ] **Step 2: Run the new tests to verify they pass**

Run: `uv run pytest tests/test_update_cmd.py::test_main_upgrade_succeeds tests/test_update_cmd.py::test_main_upgrade_fails tests/test_update_cmd.py::test_main_upgrade_already_latest tests/test_update_cmd.py::test_main_upgrade_not_installed -v`
Expected: all four PASS.

- [ ] **Step 3: Run the full test suite for this file**

Run: `uv run pytest tests/test_update_cmd.py -v`
Expected: all 11 tests PASS.

- [ ] **Step 4: Run ruff and pyright**

Run: `uv run ruff check src/marim_harness/interfaces/cli/update_cmd.py tests/test_update_cmd.py`
Expected: no errors.

Run: `uv run pyright src/marim_harness/interfaces/cli/update_cmd.py`
Expected: no errors.

- [ ] **Step 5: Commit**

```bash
git add tests/test_update_cmd.py
git commit -m "test(update): add upgrade path and edge case tests"
```

---

### Task 6: Final integration check and cleanup

- [ ] **Step 1: Run the full test suite**

Run: `uv run pytest --no-cov`
Expected: all tests pass (existing tests unaffected).

- [ ] **Step 2: Verify `--help` output**

Run: `uv run marim update --help`
Expected: argparse help text with `--check` flag described.

- [ ] **Step 3: Verify `marim --help` lists `update`**

Run: `uv run marim --help 2>&1 || true`
Expected: not required to list it (router sends unrecognized args to default_cmd), but verify no crash.

- [ ] **Step 4: Remove any leftover debug prints or comments**

Check `update_cmd.py` for any leftover placeholder code — the "not yet implemented" stub should already be gone from Task 4.

- [ ] **Step 5: Final commit if needed**

```bash
git add -A && git diff --cached --stat
# Only if changes remain:
git commit -m "chore(update): final polish and cleanup"
```
