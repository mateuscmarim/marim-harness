"""Shared identifier + frontmatter rules for skills and sub-agents, which use
the same on-disk format: a kebab-case name plus a leading YAML frontmatter
block. Kept in one place so the two discovery modules can't drift apart."""

import re

# 1-64 chars, lowercase alphanumerics and single hyphens, no leading/trailing/
# consecutive hyphens. The identifier rule for both skill and sub-agent names.
NAME_RE = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")
# Splits a leading ``---\n...\n---`` YAML block (group 1) from the body that
# follows it (group 2).
FRONTMATTER_RE = re.compile(r"^---\s*\n(.*?)\n---\s*\n?(.*)\Z", re.DOTALL)


def valid_name(name: str) -> bool:
    """True for a 1-64 char kebab-case identifier (see :data:`NAME_RE`)."""
    return bool(name) and len(name) <= 64 and NAME_RE.match(name) is not None
