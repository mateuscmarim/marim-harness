"""A configurable allow/deny policy for shell commands.

Each pattern is a regular expression matched (``re.search``) against the whole
command string. A command is blocked when it matches any denylist pattern, or
— when an allowlist is configured — when it matches none of the allowlist
patterns. Deny takes precedence over allow. An empty policy allows everything.

The policy is enforced inside the ``bash`` tool itself, so it applies uniformly
in every permission mode (auto and ask alike) and to sub-agents too — not just
at the approval prompt, which auto mode skips entirely."""

import re


def split_patterns(text: str) -> list[str]:
    """Split a config value into patterns on commas or newlines, dropping blanks.

    Commas are the natural separator in a single-line env var; newlines suit a
    multi-line config block. A pattern that genuinely needs a literal comma can
    use a character class (``[,]``) to avoid the split."""
    if not text:
        return []
    return [part.strip() for part in re.split(r"[,\n]", text) if part.strip()]


# Fallbacks for a pattern that isn't valid regex. A malformed rule must fail
# *closed*, never silently turn into an ineffective literal: a broken deny rule
# blocks everything (better a loud over-block than false protection), and a
# broken allow rule grants nothing.
_MATCH_ALL = re.compile("")        # matches any command, including ""
_MATCH_NONE = re.compile("(?!)")   # never matches


def _compile(pattern: str, *, on_error: "re.Pattern[str]") -> "re.Pattern[str]":
    """Compile a pattern, falling back to ``on_error`` if it isn't valid regex."""
    try:
        return re.compile(pattern)
    except re.error:
        return on_error


class CommandPolicy:
    """Allow/deny rules for shell commands. See the module docstring for the
    matching semantics."""

    def __init__(
        self,
        denylist: list[str] | None = None,
        allowlist: list[str] | None = None,
    ) -> None:
        self._deny_src = list(denylist or [])
        self._allow_src = list(allowlist or [])
        self._deny = [_compile(p, on_error=_MATCH_ALL) for p in self._deny_src]
        self._allow = [_compile(p, on_error=_MATCH_NONE) for p in self._allow_src]

    @classmethod
    def parse(cls, deny: str = "", allow: str = "") -> "CommandPolicy":
        """Build a policy from raw config strings (comma- or newline-separated)."""
        return cls(denylist=split_patterns(deny), allowlist=split_patterns(allow))

    def check(self, command: str) -> str | None:
        """Return a denial reason if ``command`` is blocked, else ``None``."""
        for src, rx in zip(self._deny_src, self._deny, strict=True):
            if rx.search(command):
                return f"command matches denylist pattern {src!r}"
        if self._allow and not any(rx.search(command) for rx in self._allow):
            return "command does not match any allowlist pattern"
        return None
