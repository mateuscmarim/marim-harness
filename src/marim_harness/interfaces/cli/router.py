"""Top-level CLI entry. Routes management keywords (``sessions``, ``config``,
``models``) to their command groups; everything else falls through to the
default command (TUI or headless prompt)."""

import logging
import os
import sys

from ...config import load_environment

# Reserved first-token keywords. argparse subparsers would claim the workspace
# positional, so we route manually before any parser sees the args.
_MANAGEMENT = {"sessions", "config", "models", "plugin", "mcp"}


def _setup_logging() -> None:
    """Configure root logging. DEBUG when MARIM_DEBUG=1, else WARNING."""
    level = logging.DEBUG if os.environ.get("MARIM_DEBUG") == "1" else logging.WARNING
    logging.basicConfig(
        level=level,
        format="%(name)s %(levelname)s: %(message)s",
        force=True,
    )


def main() -> None:
    load_environment()
    _setup_logging()
    argv = sys.argv[1:]
    if argv and argv[0] in _MANAGEMENT:
        # Import only the chosen management command so the common, non-agent
        # commands (config/models) don't pay for pydantic_ai via their siblings.
        from importlib import import_module

        module = import_module(f".{argv[0]}", __package__)
        raise SystemExit(module.main(argv[1:]))
    from .default_cmd import run_default

    raise SystemExit(run_default(argv))
