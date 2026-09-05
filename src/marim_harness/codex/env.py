"""Environment probes for the Codex CLI: binary, login, timeout, version floor.

Pure lookups (``shutil.which``, one ``is_file``) so the settings screen and
provider detection can call these freely; nothing here spawns a process.
"""

from __future__ import annotations

import json
import os
import re
import shutil
from pathlib import Path

CODEX_BINARY_ENV = "MARIM_CODEX_CLI_BIN"
CODEX_TIMEOUT_ENV = "MARIM_CODEX_CLI_TIMEOUT"
# Default model for `backend: codex-cli` sub-agents (the spec's `model:` wins;
# the spawn call's model= wins over both). None ⇒ the CLI's own default.
CODEX_MODEL_ENV = "MARIM_CODEX_CLI_MODEL"
_DEFAULT_TIMEOUT = 600.0
# app-server v2 (thread/turn/item vocabulary, typed approval requests) landed
# in this line; older binaries speak a different protocol and are refused.
MIN_CODEX_VERSION = (0, 152)
INSTALL_HINT = (
    "Install the Codex CLI (npm i -g @openai/codex) and run `codex login`; "
    f"marim needs codex >= {MIN_CODEX_VERSION[0]}.{MIN_CODEX_VERSION[1]}."
)

_VERSION_RE = re.compile(r"(\d+)\.(\d+)(?:\.(\d+))?")
# A `-c key=value` override path segment must be a TOML *bare* key: the
# CLI's dotted-path parser rejects quoted segments (probed on codex 0.152:
# `mcp_servers."x y".enabled=false` errors out), so a server whose name
# falls outside this set cannot be disabled by override and is reported.
_BARE_KEY_RE = re.compile(r"^[A-Za-z0-9_-]+$")
# Feature flags a marim-started app-server is launched with. `plugins` empties
# the installed-plugin set (per-plugin `plugins.<id>.enabled=false` overrides
# had no effect in the same probe); `apps` removes the built-in `codex_apps`
# connector server (`github.*` tools), which survives `plugins=false` on its
# own. Both verified against codex 0.152.1. `skip_host_skill_discovery` was
# probed too and does NOT hide user skills — skills stay a documented residual.
PLUGINS_OFF_OVERRIDE = "features.plugins=false"
APPS_OFF_OVERRIDE = "features.apps=false"
FEATURE_OVERRIDES = (PLUGINS_OFF_OVERRIDE, APPS_OFF_OVERRIDE)


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


def parse_mcp_server_names(raw: str | bytes) -> list[str]:
    """Server names from ``codex mcp list --json`` (a JSON list of objects
    carrying ``name``), in the CLI's order; ``[]`` for anything unparsable —
    the caller treats that as "nothing to disable" and logs it."""
    try:
        data = json.loads(raw)
    except (ValueError, TypeError):
        return []
    if not isinstance(data, list):
        return []
    return [
        item["name"]
        for item in data
        if isinstance(item, dict) and isinstance(item.get("name"), str)
    ]


def isolation_overrides(mcp_servers: list[str]) -> tuple[list[str], list[str]]:
    """The ``-c key=value`` argv pairs that keep a marim-started app-server
    from loading the user's own Codex extensions, plus the MCP server names
    that could NOT be disabled (non-bare-key names, see ``_BARE_KEY_RE``).

    Why per-server rather than one blanket override: Codex config overrides
    MERGE tables, so ``mcp_servers={}`` (and a ``thread/start.config`` with
    an empty table) is a no-op — every configured server still loads, which
    the first live probe showed. ``mcp_servers.<name>.enabled=false`` is
    honored per entry, hence the enumeration. Plugins and the built-in apps
    connector go through feature flags (``FEATURE_OVERRIDES``). Skills have
    no working knob today and still load — a documented residual.
    """
    argv: list[str] = []
    for flag in FEATURE_OVERRIDES:
        argv += ["-c", flag]
    skipped: list[str] = []
    for name in mcp_servers:
        if _BARE_KEY_RE.match(name):
            argv += ["-c", f"mcp_servers.{name}.enabled=false"]
        else:
            skipped.append(name)
    return argv, skipped


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
