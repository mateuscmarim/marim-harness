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
