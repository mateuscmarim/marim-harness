"""Top-level CLI entry. Routes management keywords (``sessions``, ``config``,
``models``) to their command groups; everything else falls through to the
default command (TUI or headless prompt)."""

import sys

from ..config import load_environment
from . import config as config_cmd
from . import models as models_cmd
from . import sessions as sessions_cmd
from .default_cmd import run_default

# Reserved first-token keywords. argparse subparsers would claim the workspace
# positional, so we route manually before any parser sees the args.
_MANAGEMENT = {
    "sessions": sessions_cmd.main,
    "config": config_cmd.main,
    "models": models_cmd.main,
}


def main() -> None:
    load_environment()
    argv = sys.argv[1:]
    if argv and argv[0] in _MANAGEMENT:
        raise SystemExit(_MANAGEMENT[argv[0]](argv[1:]))
    raise SystemExit(run_default(argv))
