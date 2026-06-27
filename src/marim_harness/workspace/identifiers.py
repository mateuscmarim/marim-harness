"""The shared identifier rule for marim's on-disk items — skill, sub-agent, and
plugin names all use the same kebab-case format. A dependency-free leaf module
(like ``tools/names.py``) so any package can validate a name without reaching
across into another package."""

import re

# 1-64 chars, lowercase alphanumerics and single hyphens, no leading/trailing/
# consecutive hyphens.
NAME_RE = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")


def valid_name(name: str) -> bool:
    """True for a 1-64 char kebab-case identifier (see :data:`NAME_RE`).
    Assumes a ``str``; guard untrusted input with an ``isinstance`` check first."""
    return bool(name) and len(name) <= 64 and NAME_RE.match(name) is not None
