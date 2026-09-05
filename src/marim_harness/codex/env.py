"""Environment probes for the Codex CLI: binary, login, timeout, version floor.

Pure lookups (``shutil.which``, one ``is_file``) so the settings screen and
provider detection can call these freely; nothing here spawns a process.
"""

from __future__ import annotations

import os
import re
import shutil
from pathlib import Path

CODEX_BINARY_ENV = "MARIM_CODEX_CLI_BIN"
CODEX_TIMEOUT_ENV = "MARIM_CODEX_CLI_TIMEOUT"
_DEFAULT_TIMEOUT = 600.0
# app-server v2 (thread/turn/item vocabulary, typed approval requests) landed
# in this line; older binaries speak a different protocol and are refused.
MIN_CODEX_VERSION = (0, 152)
INSTALL_HINT = (
    "Install the Codex CLI (npm i -g @openai/codex) and run `codex login`; "
    f"marim needs codex >= {MIN_CODEX_VERSION[0]}.{MIN_CODEX_VERSION[1]}."
)

_VERSION_RE = re.compile(r"(\d+)\.(\d+)(?:\.(\d+))?")


class CodexUnavailable(Exception):
    """The Codex CLI is missing, too old, or not logged in."""


def resolve_codex_binary() -> str | None:
    """Absolute path of the ``codex`` binary (``MARIM_CODEX_CLI_BIN`` override
    first, then PATH), or None."""
    return shutil.which(os.environ.get(CODEX_BINARY_ENV) or "codex")


def codex_home() -> Path:
    """``$CODEX_HOME`` or ``~/.codex`` — where the CLI keeps ``auth.json``."""
    raw = os.environ.get("CODEX_HOME")
    return Path(raw).expanduser() if raw else Path.home() / ".codex"


def codex_logged_in() -> bool:
    """Whether ``codex login`` has run (auth.json exists). Existence only — the
    token's validity surfaces as the server's own error on the first turn."""
    return (codex_home() / "auth.json").is_file()


def codex_available() -> bool:
    return resolve_codex_binary() is not None and codex_logged_in()


def codex_timeout() -> float:
    """Idle timeout (seconds) for one turn; ``MARIM_CODEX_CLI_TIMEOUT`` or 600."""
    raw = os.environ.get(CODEX_TIMEOUT_ENV, "")
    try:
        value = float(raw)
    except ValueError:
        return _DEFAULT_TIMEOUT
    return value if value > 0 else _DEFAULT_TIMEOUT


def parse_version(user_agent: str) -> tuple[int, ...] | None:
    """First ``x.y[.z]`` in an ``initialize`` ``userAgent`` string."""
    m = _VERSION_RE.search(user_agent)
    if m is None:
        return None
    return tuple(int(g) for g in m.groups() if g is not None)


def check_min_version(user_agent: str) -> None:
    """Raise ``CodexUnavailable`` unless the reported version meets the floor."""
    parsed = parse_version(user_agent)
    if parsed is None:
        raise CodexUnavailable(
            f"Could not read the codex version from {user_agent!r}. {INSTALL_HINT}"
        )
    if parsed[:2] < MIN_CODEX_VERSION:
        floor = ".".join(str(n) for n in MIN_CODEX_VERSION)
        version_str = ".".join(str(n) for n in parsed)
        raise CodexUnavailable(
            f"codex {version_str} is older than the {floor} minimum. {INSTALL_HINT}"
        )
