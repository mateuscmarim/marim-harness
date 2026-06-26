"""Shared cache helper for agents and skills discovery.

Both modules use the same pattern: stat-fingerprint the roots, check a
module-level dict, rebuild on a miss. This module owns that pattern once so
the two callers supply only what differs (how to walk roots, how to parse
entries, what the sort key is).
"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import Any


def cached_discover(
    workspace_root: object,
    roots: list[tuple[str, Path, str | None]],
    sig_fn: Callable[[list[tuple[str, Path, str | None]]], tuple],
    collect_fn: Callable[[dict, str, Path, str | None], None],
    sort_key: Callable[[Any], Any],
    cache: dict,
    defaults: dict | None = None,
) -> list:
    """Check ``cache`` for a still-valid discovery result; rebuild on a miss.

    ``sig_fn`` computes a cheap fingerprint over ``roots`` (stat-only). When
    the cached signature matches the current one, the cached list is returned
    without touching the filesystem again. On a miss, ``collect_fn`` is called
    for each root, ``defaults`` items are inserted with ``setdefault`` (so
    user roots always win), and the result is sorted by ``sort_key``."""
    sig = sig_fn(roots)
    key = str(Path(str(workspace_root)).resolve())
    cached = cache.get(key)
    if cached is not None and cached[0] == sig:
        return cached[1]

    seen: dict = {}
    for source, root, plugin in roots:
        collect_fn(seen, source, root, plugin)
    if defaults:
        for name, item in defaults.items():
            seen.setdefault(name, item)
    result = sorted(seen.values(), key=sort_key)
    cache[key] = (sig, result)
    return result
