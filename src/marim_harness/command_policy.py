"""A configurable allow/deny policy for shell commands.

Each pattern is a regular expression matched (``re.search``) against the whole
command string. A command is blocked when it matches any denylist pattern, or
— when an allowlist is configured — when it matches none of the allowlist
patterns. Deny takes precedence over allow. An empty policy allows everything.

The policy is enforced inside the ``bash`` tool itself, so it applies uniformly
in every permission mode (auto and ask alike) and to sub-agents too — not just
at the approval prompt, which auto mode skips entirely."""

import re
from typing import Optional


def split_patterns(text: str) -> list[str]:
    """Split a config value into patterns on commas or newlines, dropping blanks.

    Commas are the natural separator in a single-line env var; newlines suit a
    multi-line config block. A pattern that genuinely needs a literal comma can
    use a character class (``[,]``) to avoid the split."""
    if not text:
        return []
    return [part.strip() for part in re.split(r"[,\n]", text) if part.strip()]


def _compile(pattern: str) -> "re.Pattern[str]":
    """Compile a pattern, falling back to a literal match if it isn't valid
    regex — a malformed deny rule should still block, not silently vanish."""
    try:
        return re.compile(pattern)
    except re.error:
        return re.compile(re.escape(pattern))


class CommandPolicy:
    """Allow/deny rules for shell commands. See the module docstring for the
    matching semantics."""

    def __init__(
        self,
        denylist: Optional[list[str]] = None,
        allowlist: Optional[list[str]] = None,
    ) -> None:
        self._deny_src = list(denylist or [])
        self._allow_src = list(allowlist or [])
        self._deny = [_compile(p) for p in self._deny_src]
        self._allow = [_compile(p) for p in self._allow_src]

    @classmethod
    def parse(cls, deny: str = "", allow: str = "") -> "CommandPolicy":
        """Build a policy from raw config strings (comma- or newline-separated)."""
        return cls(denylist=split_patterns(deny), allowlist=split_patterns(allow))

    def check(self, command: str) -> Optional[str]:
        """Return a denial reason if ``command`` is blocked, else ``None``."""
        for src, rx in zip(self._deny_src, self._deny):
            if rx.search(command):
                return f"command matches denylist pattern {src!r}"
        if self._allow and not any(rx.search(command) for rx in self._allow):
            return "command does not match any allowlist pattern"
        return None
