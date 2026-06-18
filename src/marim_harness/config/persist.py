"""Persist a small set of MARIM_* settings to the global .env so they take
effect on the next launch. Update-or-append per key, preserving comments and
any unmanaged keys; the in-process os.environ is mirrored so a later
load_config() in the same process reflects the save."""

import os
from pathlib import Path
from typing import Optional

from dotenv import set_key

from .env import global_config_path


def save_env_settings(values: dict[str, str], path: Optional[Path] = None) -> Path:
    """Write each ``key=value`` in ``values`` into the global .env (or ``path``),
    creating the file and its parent directory if needed. Values are written
    unquoted. Returns the path written."""
    target = path or global_config_path()
    target.parent.mkdir(parents=True, exist_ok=True)
    target.touch(exist_ok=True)
    for key, value in values.items():
        set_key(str(target), key, value, quote_mode="never")
        os.environ[key] = value
    return target
