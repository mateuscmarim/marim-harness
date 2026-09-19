"""Tests for the marim update command."""

import sys
from io import StringIO
from unittest.mock import patch

import httpx
import pytest

from marim_harness.interfaces.cli.update import UpdateInfo

# --- _check_latest() tests ---


def test_check_latest_outdated():
    from marim_harness.interfaces.cli.update import _check_latest

    mock_response = {
        "info": {
            "version": "9.9.9",
            "release_url": "https://pypi.org/project/marim-harness/9.9.9/",
        }
    }
    with patch("httpx.get") as mock_get:
        mock_get.return_value.json.return_value = mock_response
        mock_get.return_value.raise_for_status = lambda: None
        with patch("marim_harness.interfaces.cli.update.version", return_value="0.3.0"):
            result = _check_latest()

    assert result.current == "0.3.0"
    assert result.latest == "9.9.9"
    assert result.is_outdated is True
    assert "9.9.9" in result.release_url


def test_check_latest_current():
    from marim_harness.interfaces.cli.update import _check_latest

    mock_response = {
        "info": {
            "version": "0.3.0",
            "release_url": "https://pypi.org/project/marim-harness/0.3.0/",
        }
    }
    with patch("httpx.get") as mock_get:
        mock_get.return_value.json.return_value = mock_response
        mock_get.return_value.raise_for_status = lambda: None
        with patch("marim_harness.interfaces.cli.update.version", return_value="0.3.0"):
            result = _check_latest()

    assert result.is_outdated is False


def test_check_latest_network_error():
    from marim_harness.interfaces.cli.update import _check_latest

    with (
        patch("httpx.get", side_effect=httpx.ConnectError("connection refused")),
        patch("marim_harness.interfaces.cli.update.version", return_value="0.3.0"),
        pytest.raises(RuntimeError, match="Could not reach PyPI"),
    ):
        _check_latest()


def test_check_latest_not_installed():
    from importlib.metadata import PackageNotFoundError

    from marim_harness.interfaces.cli.update import _check_latest

    with (
        patch(
            "marim_harness.interfaces.cli.update.version",
            side_effect=PackageNotFoundError,
        ),
        pytest.raises(PackageNotFoundError),
    ):
        _check_latest()


# --- _do_upgrade() tests ---


def test_do_upgrade_uv_tool_succeeds():
    from marim_harness.interfaces.cli.update import _do_upgrade

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


def test_do_upgrade_falls_back_to_pip_when_not_a_uv_tool():
    from marim_harness.interfaces.cli.update import _do_upgrade

    uv_upgrade_result = type("Result", (), {"returncode": 1})()
    uv_list_result = type("Result", (), {"returncode": 0, "stdout": "", "stderr": ""})()
    pip_result = type("Result", (), {"returncode": 0})()
    with patch(
        "subprocess.run", side_effect=[uv_upgrade_result, uv_list_result, pip_result]
    ) as mock_run:
        result = _do_upgrade()
        assert result == 0
        assert mock_run.call_count == 3
        first_args = mock_run.call_args_list[0][0][0]
        second_args = mock_run.call_args_list[1][0][0]
        third_args = mock_run.call_args_list[2][0][0]
        assert first_args[0] == "uv"
        assert second_args[:3] == ["uv", "tool", "list"]
        assert "pip" in third_args


def test_do_upgrade_reinstalls_stale_uv_tool_source():
    """`uv tool upgrade` reuses the receipt's source, which can be a local
    wheel path (dev build, release scratchpad artifact) that no longer
    exists on disk. When marim-harness is a known uv tool, retry with a
    forced reinstall by name so uv re-resolves from PyPI instead."""
    from marim_harness.interfaces.cli.update import _do_upgrade

    uv_upgrade_result = type("Result", (), {"returncode": 1})()
    uv_list_result = type(
        "Result",
        (),
        {
            "returncode": 0,
            "stdout": "marim-harness v0.10.0 [extras: serve, tui, workflows, lsp-python]\n",
            "stderr": "",
        },
    )()
    reinstall_result = type("Result", (), {"returncode": 0})()
    with patch(
        "subprocess.run",
        side_effect=[uv_upgrade_result, uv_list_result, reinstall_result],
    ) as mock_run:
        result = _do_upgrade()
        assert result == 0
        assert mock_run.call_count == 3
        reinstall_args = mock_run.call_args_list[2][0][0]
        assert reinstall_args[:4] == ["uv", "tool", "install", "--force"]
        assert "--reinstall" in reinstall_args
        assert "marim-harness[serve,tui,workflows,lsp-python]" in reinstall_args


def test_do_upgrade_reinstall_without_extras():
    from marim_harness.interfaces.cli.update import _do_upgrade

    uv_upgrade_result = type("Result", (), {"returncode": 1})()
    uv_list_result = type(
        "Result",
        (),
        {"returncode": 0, "stdout": "marim-harness v0.10.0\n", "stderr": ""},
    )()
    reinstall_result = type("Result", (), {"returncode": 0})()
    with patch(
        "subprocess.run",
        side_effect=[uv_upgrade_result, uv_list_result, reinstall_result],
    ) as mock_run:
        result = _do_upgrade()
        assert result == 0
        reinstall_args = mock_run.call_args_list[2][0][0]
        assert "marim-harness" in reinstall_args
        assert not any("[" in arg for arg in reinstall_args)


def test_do_upgrade_reinstall_fails_falls_back_to_pip():
    from marim_harness.interfaces.cli.update import _do_upgrade

    uv_upgrade_result = type("Result", (), {"returncode": 1})()
    uv_list_result = type(
        "Result",
        (),
        {"returncode": 0, "stdout": "marim-harness v0.10.0 [extras: tui]\n", "stderr": ""},
    )()
    reinstall_result = type("Result", (), {"returncode": 1})()
    pip_result = type("Result", (), {"returncode": 0})()
    with patch(
        "subprocess.run",
        side_effect=[uv_upgrade_result, uv_list_result, reinstall_result, pip_result],
    ) as mock_run:
        result = _do_upgrade()
        assert result == 0
        assert mock_run.call_count == 4
        assert "pip" in mock_run.call_args_list[3][0][0]


def test_do_upgrade_pip_fails():
    from marim_harness.interfaces.cli.update import _do_upgrade

    with patch("subprocess.run") as mock_run:
        mock_run.return_value.returncode = 1
        result = _do_upgrade()
        assert result == 1


def test_do_upgrade_uv_not_found():
    from marim_harness.interfaces.cli.update import _do_upgrade

    pip_result = type("Result", (), {"returncode": 0})()
    expected = [
        "/usr/bin/python",
        "-m",
        "pip",
        "install",
        "--upgrade",
        "marim-harness",
    ]
    with (
        patch("subprocess.run", side_effect=[FileNotFoundError, pip_result]) as mock_run,
        patch.object(sys, "executable", "/usr/bin/python"),
    ):
        _do_upgrade()
        assert mock_run.call_count == 2
        assert mock_run.call_args_list[0][0][0][0] == "uv"
        assert mock_run.call_args_list[1][0][0] == expected


def _result(returncode: int, stdout: str = ""):
    return type("Result", (), {"returncode": returncode, "stdout": stdout, "stderr": ""})()


def test_do_upgrade_reads_the_version_back_when_a_target_is_known():
    """With a target version, a zero exit from `uv tool upgrade` is confirmed
    against `uv tool list` before it counts."""
    from marim_harness.interfaces.cli.update import _do_upgrade

    with patch(
        "subprocess.run",
        side_effect=[_result(0), _result(0, "marim-harness v0.15.0 [extras: tui]\n")],
    ) as mock_run:
        assert _do_upgrade("0.15.0", out=StringIO()) == 0
        assert mock_run.call_count == 2
        assert mock_run.call_args_list[1][0][0][:3] == ["uv", "tool", "list"]


def test_do_upgrade_reinstalls_when_upgrade_is_a_noop_short_of_target():
    """A receipt pinned to a local wheel makes `uv tool upgrade` succeed with
    "Nothing to upgrade" and leave the old version installed. That is the
    same stale-source case as a failed upgrade: reinstall by name from PyPI
    with the original extras."""
    from marim_harness.interfaces.cli.update import _do_upgrade

    out = StringIO()
    with patch(
        "subprocess.run",
        side_effect=[
            _result(0),
            _result(0, "marim-harness v0.14.0 [extras: serve, tui]\n"),
            _result(0),
        ],
    ) as mock_run:
        assert _do_upgrade("0.15.0", out=out) == 0
        assert mock_run.call_count == 3
        reinstall_args = mock_run.call_args_list[2][0][0]
        assert reinstall_args[:4] == ["uv", "tool", "install", "--force"]
        assert "marim-harness[serve,tui]" in reinstall_args
    assert "left marim-harness at 0.14.0" in out.getvalue()


def test_do_upgrade_noop_upgrade_of_a_non_uv_tool_is_trusted():
    from marim_harness.interfaces.cli.update import _do_upgrade

    with patch("subprocess.run", side_effect=[_result(0), _result(0, "")]) as mock_run:
        assert _do_upgrade("0.15.0", out=StringIO()) == 0
        assert mock_run.call_count == 2


# --- main() tests ---


def test_main_check_outdated():
    from marim_harness.interfaces.cli.update import main

    info = UpdateInfo(
        current="0.3.0",
        latest="9.9.9",
        release_url="https://pypi.org/project/marim-harness/9.9.9/",
    )
    with patch("marim_harness.interfaces.cli.update._check_latest", return_value=info):
        out = StringIO()
        err = StringIO()
        result = main(["--check"], out=out, err=err)
        output = out.getvalue()
        assert result == 0
        assert "0.3.0" in output
        assert "9.9.9" in output


def test_main_check_current():
    from marim_harness.interfaces.cli.update import main

    info = UpdateInfo(
        current="0.3.0",
        latest="0.3.0",
        release_url="https://pypi.org/project/marim-harness/0.3.0/",
    )
    with patch("marim_harness.interfaces.cli.update._check_latest", return_value=info):
        out = StringIO()
        err = StringIO()
        result = main(["--check"], out=out, err=err)
        output = out.getvalue()
        assert result == 0
        assert "already the latest" in output.lower()


def test_main_check_network_error():
    from marim_harness.interfaces.cli.update import main

    with patch(
        "marim_harness.interfaces.cli.update._check_latest",
        side_effect=RuntimeError("Could not reach PyPI"),
    ):
        out = StringIO()
        err = StringIO()
        result = main(["--check"], out=out, err=err)
        assert result == 1
        assert "Could not reach PyPI" in err.getvalue()


def test_main_check_not_installed():
    from importlib.metadata import PackageNotFoundError

    from marim_harness.interfaces.cli.update import main

    with patch(
        "marim_harness.interfaces.cli.update._check_latest",
        side_effect=PackageNotFoundError,
    ):
        out = StringIO()
        err = StringIO()
        result = main(["--check"], out=out, err=err)
        assert result == 1
        assert "not installed" in out.getvalue().lower()


def test_main_upgrade_succeeds():
    from marim_harness.interfaces.cli.update import _ToolEntry, main

    info = UpdateInfo(
        current="0.3.0",
        latest="9.9.9",
        release_url="https://pypi.org/project/marim-harness/9.9.9/",
    )
    with (
        patch("marim_harness.interfaces.cli.update._check_latest", return_value=info),
        patch("marim_harness.interfaces.cli.update._do_upgrade", return_value=0) as do_upgrade,
        patch(
            "marim_harness.interfaces.cli.update._uv_tool_entry",
            return_value=_ToolEntry(version="9.9.9", extras=[]),
        ),
    ):
        out = StringIO()
        err = StringIO()
        result = main([], out=out, err=err)
        output = out.getvalue()
        assert result == 0
        assert "Upgraded" in output
        assert do_upgrade.call_args[0][0] == "9.9.9"


def test_main_upgrade_does_not_claim_a_version_it_did_not_install():
    """The bug: `uv tool upgrade` exited 0 with "Nothing to upgrade" and the
    command printed "Upgraded to 0.15.0" over a binary still at 0.14.0."""
    from marim_harness.interfaces.cli.update import _ToolEntry, main

    info = UpdateInfo(current="0.14.0", latest="0.15.0", release_url="")
    with (
        patch("marim_harness.interfaces.cli.update._check_latest", return_value=info),
        patch("marim_harness.interfaces.cli.update._do_upgrade", return_value=0),
        patch(
            "marim_harness.interfaces.cli.update._uv_tool_entry",
            return_value=_ToolEntry(version="0.14.0", extras=["tui"]),
        ),
    ):
        out = StringIO()
        err = StringIO()
        result = main([], out=out, err=err)
    assert result == 1
    assert "Upgraded" not in out.getvalue()
    assert "still 0.14.0" in err.getvalue()
    assert "uv tool install --force --reinstall marim-harness[tui]" in err.getvalue()


def test_main_upgrade_fails():
    from marim_harness.interfaces.cli.update import main

    info = UpdateInfo(
        current="0.3.0",
        latest="9.9.9",
        release_url="https://pypi.org/project/marim-harness/9.9.9/",
    )
    with (
        patch("marim_harness.interfaces.cli.update._check_latest", return_value=info),
        patch("marim_harness.interfaces.cli.update._do_upgrade", return_value=1),
    ):
        out = StringIO()
        err = StringIO()
        result = main([], out=out, err=err)
        assert result == 1


def test_main_upgrade_already_latest():
    from marim_harness.interfaces.cli.update import main

    info = UpdateInfo(
        current="0.3.0",
        latest="0.3.0",
        release_url="https://pypi.org/project/marim-harness/0.3.0/",
    )
    with (
        patch("marim_harness.interfaces.cli.update._check_latest", return_value=info),
        patch("marim_harness.interfaces.cli.update._do_upgrade") as mock_upgrade,
    ):
        out = StringIO()
        err = StringIO()
        result = main([], out=out, err=err)
        output = out.getvalue()
        assert result == 0
        assert "already the latest" in output.lower()
        mock_upgrade.assert_not_called()


def test_main_upgrade_not_installed():
    from importlib.metadata import PackageNotFoundError

    from marim_harness.interfaces.cli.update import main

    with patch(
        "marim_harness.interfaces.cli.update._check_latest",
        side_effect=PackageNotFoundError,
    ):
        out = StringIO()
        err = StringIO()
        result = main([], out=out, err=err)
        assert result == 1
        assert "not installed" in out.getvalue().lower()
