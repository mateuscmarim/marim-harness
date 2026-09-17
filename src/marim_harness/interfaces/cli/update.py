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


def _uv_tool_extras(name: str) -> list[str] | None:
    """Return the extras `name` is currently installed with as a uv tool.

    Returns None when `name` isn't a known uv tool at all (as distinct from
    a known uv tool installed with no extras, which returns []).
    """
    result = subprocess.run(
        ["uv", "tool", "list", "--show-extras"],
        check=False,
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        return None
    match = re.search(
        rf"^{re.escape(name)} v\S+(?: \[extras: ([^\]]+)\])?$",
        result.stdout,
        re.MULTILINE,
    )
    if match is None:
        return None
    extras = match.group(1)
    return [extra.strip() for extra in extras.split(",")] if extras else []


def _do_upgrade() -> int:
    """Upgrade marim-harness: try `uv tool upgrade`, then — if it's a known uv
    tool — a forced reinstall from PyPI, then pip as a last resort.

    `uv tool upgrade` reuses the source recorded in the tool's install
    receipt. When marim-harness was installed from a local wheel path (a dev
    build, a release scratchpad artifact) that path can go stale once the
    file is cleaned up, and `uv tool upgrade` fails trying to reuse it even
    though the package is readily available on PyPI. Reinstalling by name
    forces uv to re-resolve from PyPI instead, preserving whatever extras
    were originally installed.
    """
    try:
        result = subprocess.run(
            ["uv", "tool", "upgrade", "marim-harness"],
            check=False,
        )
    except FileNotFoundError:
        result = None

    if result is not None and result.returncode == 0:
        return 0

    if result is not None:
        extras = _uv_tool_extras("marim-harness")
        if extras is not None:
            spec = f"marim-harness[{','.join(extras)}]" if extras else "marim-harness"
            result = subprocess.run(
                ["uv", "tool", "install", "--force", "--reinstall", spec],
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
    code = _do_upgrade()
    if code == 0:
        print(f"Upgraded to marim-harness {info.latest}.", file=out)
    return code
