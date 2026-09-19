"""`marim update` — upgrade the installed marim-harness package."""

import argparse
import re
import subprocess
import sys
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


@dataclass(frozen=True)
class _ToolEntry:
    """What `uv tool list` knows about an installed tool: its version and
    the extras it was installed with."""

    version: str
    extras: list[str]

    def spec(self, name: str) -> str:
        return f"{name}[{','.join(self.extras)}]" if self.extras else name


def _uv_tool_entry(name: str) -> _ToolEntry | None:
    """Return `name`'s installed version and extras as a uv tool.

    Returns None when `name` isn't a known uv tool at all (as distinct from
    a known uv tool installed with no extras, which returns an entry with
    `extras == []`).
    """
    try:
        result = subprocess.run(
            ["uv", "tool", "list", "--show-extras"],
            check=False,
            capture_output=True,
            text=True,
        )
    except FileNotFoundError:
        return None
    if result.returncode != 0:
        return None
    match = re.search(
        rf"^{re.escape(name)} v(\S+)(?: \[extras: ([^\]]+)\])?$",
        result.stdout,
        re.MULTILINE,
    )
    if match is None:
        return None
    extras = match.group(2)
    return _ToolEntry(
        version=match.group(1),
        extras=[extra.strip() for extra in extras.split(",")] if extras else [],
    )


def _uv_tool_extras(name: str) -> list[str] | None:
    """The extras `name` is installed with as a uv tool, or None when it is
    not a uv tool (see `_uv_tool_entry`)."""
    entry = _uv_tool_entry(name)
    return None if entry is None else entry.extras


def _do_upgrade(target: str | None = None, *, out=None) -> int:
    """Upgrade marim-harness: try `uv tool upgrade`, then — if it's a known uv
    tool that did not reach `target` — a forced reinstall from PyPI, then pip
    as a last resort.

    `uv tool upgrade` reuses the source recorded in the tool's install
    receipt. When marim-harness was installed from a local wheel path (a dev
    build, a release scratchpad artifact) that source can only ever yield
    the version baked into the file: if the file is gone `uv tool upgrade`
    FAILS trying to reuse it, and if the file is still there it SUCCEEDS with
    "Nothing to upgrade" and the tool stays at the old version. Both are
    handled the same way — reinstalling by name forces uv to re-resolve from
    PyPI, preserving whatever extras were originally installed. That is why
    a zero exit from `uv tool upgrade` is not taken at its word when a
    `target` version is known: the installed version is read back and only
    a tool that actually reached the target counts as upgraded.
    """
    out = sys.stdout if out is None else out
    try:
        result = subprocess.run(
            ["uv", "tool", "upgrade", "marim-harness"],
            check=False,
        )
    except FileNotFoundError:
        result = None

    if result is not None and result.returncode == 0 and target is None:
        return 0

    if result is not None:
        entry = _uv_tool_entry("marim-harness")
        if result.returncode == 0 and (entry is None or entry.version == target):
            return 0
        if entry is not None:
            if result.returncode == 0:
                print(
                    f"uv tool upgrade left marim-harness at {entry.version} (its install "
                    "source is pinned to that version); reinstalling from PyPI...",
                    file=out,
                )
            result = subprocess.run(
                ["uv", "tool", "install", "--force", "--reinstall", entry.spec("marim-harness")],
                check=False,
            )
            if result.returncode == 0:
                return 0

    result = subprocess.run(
        [sys.executable, "-m", "pip", "install", "--upgrade", "marim-harness"],
        check=False,
    )
    return result.returncode


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
                f"marim-harness {info.current} is outdated — {info.latest} is available.",
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
    code = _do_upgrade(info.latest, out=out)
    if code != 0:
        return code
    return _report_upgrade(info.latest, out=out, err=err)


def _report_upgrade(latest: str, *, out, err) -> int:
    """Never report an upgrade the install did not actually deliver: a uv
    tool is read back after the fact, and "Upgraded" is printed only when it
    is at `latest`. (A pip install has no receipt to read back; its exit
    code is the only word we have.)"""
    entry = _uv_tool_entry("marim-harness")
    if entry is not None and entry.version != latest:
        print(
            f"marim-harness is still {entry.version} after the upgrade (expected "
            f"{latest}). Reinstall it by name to re-resolve from PyPI:\n"
            f"  uv tool install --force --reinstall {entry.spec('marim-harness')}",
            file=err,
        )
        return 1
    print(f"Upgraded to marim-harness {latest}.", file=out)
    return 0
