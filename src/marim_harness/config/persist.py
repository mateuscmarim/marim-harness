"""Persist a small set of MARIM_* settings to the global .env so they take
effect on the next launch. Update-or-append per key, preserving comments and
any unmanaged keys; the in-process os.environ is mirrored so a later
load_config() in the same process reflects the save.

This module is the single source of truth for *how* a managed .env line is
written. Both writers — the TUI's ``save_env_settings`` and the CLI's
``marim config set`` — route through ``write_env_values`` so a value with a
space or a ``#`` round-trips and the file lands atomically with secret-safe
permissions. (An earlier version had the TUI path use ``set_key(...,
quote_mode="never")``, a non-atomic in-place write that silently truncated such
values at the first ``#``.)"""

import contextlib
import os
from collections.abc import Iterable
from pathlib import Path

from .env import global_config_path


def _format_value(value: str) -> str:
    """Render a value for a dotenv line, quoting only when a bare value would not
    survive reload. dotenv strips an unquoted value at the first ``#`` (inline
    comment) and trims surrounding whitespace, so a value containing whitespace,
    ``#``, or quote chars must be double-quoted (with ``\\`` and ``"`` escaped).
    Simple values (model ids, booleans, keys) stay unquoted as before."""
    if any(c in value for c in ' \t#"\'\n\r'):
        escaped = value.replace("\\", "\\\\").replace('"', '\\"')
        return f'"{escaped}"'
    return value


def _secure(path: Path) -> None:
    """Restrict the .env file to owner read/write (0600). It holds secrets such
    as OPENROUTER_API_KEY, so it must not be world-readable. Best-effort: chmod
    is meaningless on platforms (e.g. Windows) whose filesystems don't carry
    POSIX permission bits, so a failure there is swallowed rather than breaking
    the save."""
    with contextlib.suppress(OSError, NotImplementedError):
        os.chmod(path, 0o600)


def _line_key(raw: str) -> str:
    """The env-var name a dotenv line assigns (`KEY=` or `export KEY=`), or ""
    for comments/blank lines."""
    stripped = raw.lstrip()
    head = stripped[len("export ") :] if stripped.startswith("export ") else stripped
    return head.split("=", 1)[0].strip()


def write_env_values(
    values: dict[str, str], target: Path, *, drop: Iterable[str] = ()
) -> None:
    """Write each ``key=value`` in ``values`` into the dotenv file at ``target``,
    updating a key's line in place if it already exists and preserving every
    other line (comments and unmanaged keys). Any key in ``drop`` has its line
    removed in the same atomic write — used when a save RENAMES a setting
    (e.g. MARIM_MAX_CONTEXT_TOKENS → MARIM_CONTEXT_BUDGET), so the retired
    key can't linger and shadow or nag about the new one. Writes the whole
    file atomically (temp file + ``replace``) so a crash mid-write can't
    truncate the config, and secures the result to 0600 since it may hold
    secrets.

    This is the one correct writer; ``save_env_settings`` (TUI) and the CLI
    ``_persist`` both delegate here so their on-disk format and durability match."""
    target.parent.mkdir(parents=True, exist_ok=True)

    existing = target.read_text(encoding="utf-8").splitlines() if target.exists() else []
    dropped = set(drop)
    new_lines = [line for line in existing if _line_key(line) not in dropped]

    for key, value in values.items():
        line = f"{key}={_format_value(value)}"
        replaced = False
        for i, raw in enumerate(new_lines):
            if _line_key(raw) == key:
                new_lines[i] = line
                replaced = True
                break
        if not replaced:
            new_lines.append(line)

    tmp = target.with_name(target.name + ".tmp")
    tmp.write_text("\n".join(new_lines) + "\n", encoding="utf-8")
    # Secure the temp file before the rename so the secret is never briefly
    # exposed under a world-readable mode at the final path.
    _secure(tmp)
    tmp.replace(target)
    _secure(target)


def save_env_settings(
    values: dict[str, str], path: Path | None = None, *, drop: Iterable[str] = ()
) -> Path:
    """Write each ``key=value`` in ``values`` into the global .env (or ``path``),
    creating the file and its parent directory if needed. Values are quoted only
    when needed to survive reload, the write is atomic, and the file is secured
    to 0600. Mirrors each value into ``os.environ`` so a later ``load_config()``
    in the same process reflects the save; keys in ``drop`` are removed from
    both the file and ``os.environ`` (a renamed setting must not survive as
    its deprecated alias anywhere the loader looks). Returns the path written."""
    target = path or global_config_path()
    write_env_values(values, target, drop=drop)
    for key in drop:
        os.environ.pop(key, None)
    for key, value in values.items():
        os.environ[key] = value
    return target
