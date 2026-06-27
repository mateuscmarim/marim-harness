"""Shared frontmatter parsing for skills and sub-agents, which carry the same
on-disk format: a leading YAML frontmatter block followed by a body. Kept in one
place so the two discovery modules can't drift apart. (The kebab-case name rule
they also share lives in :mod:`marim_harness.workspace.identifiers`, since
plugins validate names too but have no frontmatter.)"""

import re

# Splits a leading ``---\n...\n---`` YAML block (group 1) from the body that
# follows it (group 2).
FRONTMATTER_RE = re.compile(r"^---\s*\n(.*?)\n---\s*\n?(.*)\Z", re.DOTALL)
