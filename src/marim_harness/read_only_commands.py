"""Classify a shell command as read-only, for plan mode.

Plan mode lets the agent research before presenting a plan. Read-only commands
(git status/log/diff, ls, cat, grep, ...) are useful research and are allowed
through the approval gate; anything that could mutate the workspace is denied.

NOT A SANDBOX. Like ``command_policy.py`` this is a best-effort nudge over the
raw command string, not a security boundary: a determined caller can evade it
(``$(echo rm) -rf``, pipes, eval). It exists to keep honest planning honest, not
to contain a hostile command. For real isolation, sandbox the host."""

import re

# Shell metacharacters that enable chaining, command substitution, or writing.
# Any of these and we refuse to call the command read-only: cheaply proving every
# chained segment is safe is not worth it, and research rarely needs them.
_UNSAFE = re.compile(r"[;&|<>`\n]|\$\(")

# Single programs that only read. ``echo``/``printf`` are read-only on their own
# (a write needs a redirection, which ``_UNSAFE`` already rejects).
_ALLOWED_PROGRAMS = frozenset(
    {
        "ls", "cat", "head", "tail", "wc", "file", "stat", "tree", "pwd",
        "date", "whoami", "hostname", "uname", "which", "type", "rg", "grep",
        "find", "fd", "ack", "echo", "printf",
    }
)

# find primaries that execute a program or write to disk. `-exec ... \;` is
# already caught by the `;` in _UNSAFE, but the `+` terminator and -delete are
# not — so screen find's arguments explicitly. Conservative by design.
_FIND_MUTATING = frozenset(
    {"-delete", "-exec", "-execdir", "-ok", "-okdir",
     "-fprint", "-fprint0", "-fls", "-fprintf"}
)

# ``git`` is read-only only for these subcommands.
_ALLOWED_GIT_SUBCMDS = frozenset(
    {
        "status", "log", "diff", "show", "branch", "remote", "tag",
        "describe", "blame", "rev-parse", "ls-files", "shortlog",
    }
)


def is_read_only(command: str) -> bool:
    """True when ``command`` is a single read-only command with no shell
    metacharacters. Conservative by design (see module docstring): chaining,
    substitution, redirection, or an unknown program is treated as not
    read-only."""
    cmd = (command or "").strip()
    if not cmd or _UNSAFE.search(cmd):
        return False
    parts = cmd.split()
    program = parts[0]
    if program == "git":
        return len(parts) >= 2 and parts[1] in _ALLOWED_GIT_SUBCMDS
    if program == "find":
        return not any(tok in _FIND_MUTATING for tok in parts[1:])
    return program in _ALLOWED_PROGRAMS
