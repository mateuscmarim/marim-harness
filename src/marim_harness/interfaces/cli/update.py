"""`marim update` — upgrade the installed marim-harness package."""

import argparse
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
