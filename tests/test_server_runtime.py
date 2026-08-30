"""The daemon's runtime.json: where it is listening, for a client to find."""

import json
import os
from pathlib import Path
from urllib.parse import urlsplit

from marim_harness.server.runtime import (
    DaemonRuntime,
    clear_runtime,
    format_base_url,
    read_runtime,
    runtime_path,
    write_runtime,
)


def test_format_base_url_ipv4() -> None:
    assert format_base_url("127.0.0.1", 8642) == "http://127.0.0.1:8642"


def test_format_base_url_all_interfaces() -> None:
    assert format_base_url("0.0.0.0", 8642) == "http://0.0.0.0:8642"


def test_format_base_url_brackets_bare_ipv6() -> None:
    assert format_base_url("::1", 8643) == "http://[::1]:8643"


def test_format_base_url_is_idempotent_on_already_bracketed_ipv6() -> None:
    assert format_base_url("[::1]", 8643) == "http://[::1]:8643"


def test_format_base_url_hostname() -> None:
    assert format_base_url("example.com", 80) == "http://example.com:80"


def test_format_base_url_ipv6_round_trips_through_urlsplit() -> None:
    assert urlsplit(format_base_url("::1", 8643)).hostname == "::1"


def test_daemon_runtime_url_brackets_ipv6_host() -> None:
    runtime = DaemonRuntime(
        host="::1", port=8643, pid=1, started="2026-08-30T00:00:00+00:00"
    )
    assert runtime.url == "http://[::1]:8643"


def test_write_then_read_roundtrips(tmp_path: Path) -> None:
    write_runtime(tmp_path, host="127.0.0.1", port=8642)
    runtime = read_runtime(tmp_path)
    assert isinstance(runtime, DaemonRuntime)
    assert runtime.host == "127.0.0.1"
    assert runtime.port == 8642
    assert runtime.pid > 0
    assert runtime.started


def test_url_is_reconstructable(tmp_path: Path) -> None:
    write_runtime(tmp_path, host="127.0.0.1", port=8642)
    assert read_runtime(tmp_path).url == "http://127.0.0.1:8642"


def test_write_creates_the_state_dir(tmp_path: Path) -> None:
    target = tmp_path / "does" / "not" / "exist"
    write_runtime(target, host="127.0.0.1", port=1)
    assert runtime_path(target).exists()


def test_read_is_none_when_absent(tmp_path: Path) -> None:
    assert read_runtime(tmp_path) is None


def test_read_is_none_on_corrupt_json(tmp_path: Path) -> None:
    runtime_path(tmp_path).parent.mkdir(parents=True, exist_ok=True)
    runtime_path(tmp_path).write_text("{{{not json")
    assert read_runtime(tmp_path) is None


def test_read_is_none_when_fields_are_missing(tmp_path: Path) -> None:
    runtime_path(tmp_path).parent.mkdir(parents=True, exist_ok=True)
    runtime_path(tmp_path).write_text(json.dumps({"host": "127.0.0.1"}))
    assert read_runtime(tmp_path) is None


def test_clear_removes_the_file(tmp_path: Path) -> None:
    write_runtime(tmp_path, host="127.0.0.1", port=8642)
    clear_runtime(tmp_path)
    assert read_runtime(tmp_path) is None


def test_clear_is_a_noop_when_absent(tmp_path: Path) -> None:
    clear_runtime(tmp_path)  # must not raise


def test_clear_leaves_another_daemons_record_alone(tmp_path: Path) -> None:
    """A second `marim serve` that fails to start runs the same shutdown path.
    It must not delete the record of the daemon that is still running."""
    write_runtime(tmp_path, host="127.0.0.1", port=8642)
    record = json.loads(runtime_path(tmp_path).read_text())
    record["pid"] = os.getpid() + 1  # a foreign, live-looking daemon
    runtime_path(tmp_path).write_text(json.dumps(record))

    clear_runtime(tmp_path)

    assert runtime_path(tmp_path).exists()
    survivor = read_runtime(tmp_path)
    assert survivor is not None
    assert survivor.pid == os.getpid() + 1
    assert survivor.port == 8642
