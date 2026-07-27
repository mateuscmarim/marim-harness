"""Top-level CLI entry. Routes management keywords (``sessions``, ``config``,
``models``) to their command groups; everything else falls through to the
default command (TUI or headless prompt)."""

import logging
import os
import sys

from ...config import load_environment

# Reserved first-token keywords. argparse subparsers would claim the workspace
# positional, so we route manually before any parser sees the args.
_MANAGEMENT = {"sessions", "config", "models", "plugin", "mcp", "serve", "trust"}

# Keyword -> submodule name, for the one case where they differ: the module
# is named ``trust_cmd`` (not ``trust``) so a bare `import trust` anywhere
# near this package unambiguously means the top-level ``marim_harness.trust``
# predicate module, never this CLI command group. Every other keyword here
# still maps to a same-named module.
_MODULE_NAMES = {"trust": "trust_cmd"}


def _setup_logging() -> None:
    """Configure root logging. DEBUG when MARIM_DEBUG=1, else WARNING."""
    level = logging.DEBUG if os.environ.get("MARIM_DEBUG") == "1" else logging.WARNING
    logging.basicConfig(
        level=level,
        format="%(name)s %(levelname)s: %(message)s",
        force=True,
    )


def route_logging_to_file():
    """Swap the root logger's stderr handler for a file handler, returning the
    log path (or None if it couldn't be opened).

    ``_setup_logging`` (run once at startup) installs a ``StreamHandler`` bound to
    ``sys.stderr`` *as it exists then* — the real terminal. The TUI launches
    afterwards and Textual swaps ``sys.stderr`` for its own redirect, but that
    handler still holds the original terminal stream. So any WARNING+ record (a
    logged httpx error, an asyncio "task exception was never retrieved", a library
    warning) writes straight to the real tty, painting over the live Textual
    screen. Redirecting to a file *before* the screen is taken keeps the logs
    without corrupting the display. Headless deliberately keeps the stderr handler,
    where logs-on-stderr is the right behavior."""
    from ...config import config_dir

    root = logging.getLogger()
    try:
        path = config_dir() / "marim.log"
        path.parent.mkdir(parents=True, exist_ok=True)
        handler = logging.FileHandler(path, encoding="utf-8")
    except OSError:
        return None  # can't open the file — leave logging as-is rather than crash
    handler.setFormatter(
        logging.Formatter("%(asctime)s %(name)s %(levelname)s: %(message)s")
    )
    # Drop the stderr StreamHandler(s) basicConfig installed; closing a
    # StreamHandler flushes it without closing the underlying sys.stderr.
    for existing in root.handlers[:]:
        root.removeHandler(existing)
        existing.close()
    root.addHandler(handler)
    return path


def main() -> None:
    load_environment()
    _setup_logging()
    argv = sys.argv[1:]
    if argv and argv[0] in _MANAGEMENT:
        # Import only the chosen management command so the common, non-agent
        # commands (config/models) don't pay for pydantic_ai via their siblings.
        from importlib import import_module

        module = import_module(f".{_MODULE_NAMES.get(argv[0], argv[0])}", __package__)
        raise SystemExit(module.main(argv[1:]))
    from .default_cmd import run_default

    raise SystemExit(run_default(argv))
