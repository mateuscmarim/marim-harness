"""Classify a shell command as read-only, for plan mode.

Plan mode lets the agent research before presenting a plan. Read-only commands
(git status/log/diff, ls, cat, grep, ...) are useful research and are allowed
through the approval gate; anything that could mutate the workspace is denied.

NOT A SANDBOX. Like ``command_policy.py`` this is a best-effort nudge over the
raw command string, not a security boundary: a determined caller can evade it
(``$(echo rm) -rf``, pipes, eval). It exists to keep honest planning honest, not
to contain a hostile command. For real isolation, sandbox the host."""

import re
import shlex

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

# fd flags that execute a program per match (`-x`/`--exec`, `-X`/`--exec-batch`),
# same class as find's `-exec`: no shell metachars for _UNSAFE to catch, so
# screen the tokens explicitly, including the `--exec=cmd` / `--exec-batch=cmd`
# joined forms.
_FD_MUTATING = frozenset({"-x", "--exec", "-X", "--exec-batch"})
_FD_MUTATING_PREFIXES = ("--exec=", "--exec-batch=")

# rg flags that run an arbitrary preprocessor/child command per file (`--pre`,
# `--pre-glob` invoke a preprocessor; `--hostname-bin`, added in ripgrep 14,
# invokes an arbitrary executable to supply a fake hostname) — same hole as
# fd's -x/--exec. rg has no short form for any of these. (`-z`/`--search-zip`
# is deliberately left allowed: it only shells out to a small fixed set of
# well-known decompressors by extension, not an arbitrary caller-supplied
# command.)
_RG_MUTATING_PREFIXES = ("--pre=", "--pre-glob=", "--hostname-bin=")
_RG_MUTATING = frozenset({"--pre", "--pre-glob", "--hostname-bin"})

# ``git`` is read-only only for these subcommands. branch/tag/remote are dual
# use — they list by default but mutate with the right arguments — so
# ``_git_is_read_only`` screens their arguments instead of trusting the name.
_ALLOWED_GIT_SUBCMDS = frozenset(
    {
        "status", "log", "diff", "show", "branch", "remote", "tag",
        "describe", "blame", "rev-parse", "ls-files", "shortlog",
    }
)

# The only ``git branch`` / ``git tag`` arguments accepted as read-only: bare
# listing flags. Anything else — a positional (creates a branch/tag), a delete/
# move flag, even a flag that takes a separate value — is treated as mutating.
# Conservative by design; research needs no more than these.
_BRANCH_SAFE_FLAGS = frozenset(
    {"-a", "--all", "-r", "--remotes", "-v", "-vv", "--verbose",
     "-l", "--list", "--show-current"}
)
_TAG_SAFE_FLAGS = frozenset({"-l", "--list"})

# ``git remote`` sub-actions that only read; add/remove/rename/set-url/prune/
# update all mutate the repo or the network config.
_REMOTE_SAFE_ACTIONS = frozenset({"-v", "--verbose", "show", "get-url"})


def _git_is_read_only(args: list[str]) -> bool:
    """True when ``git <args...>`` only reads. ``args`` excludes ``git``."""
    if not args or args[0] not in _ALLOWED_GIT_SUBCMDS:
        return False
    sub, rest = args[0], args[1:]
    # --output/-o redirect diff-family output to a file — a write with no
    # shell redirection for _UNSAFE to catch (verified live: `git diff
    # --output=x` creates x). Denied for every subcommand.
    if any(tok in ("-o", "--output") or tok.startswith("--output=") for tok in rest):
        return False
    if sub == "branch":
        return all(tok in _BRANCH_SAFE_FLAGS for tok in rest)
    if sub == "tag":
        return all(tok in _TAG_SAFE_FLAGS for tok in rest)
    if sub == "remote":
        return not rest or rest[0] in _REMOTE_SAFE_ACTIONS
    return True


def _fd_is_read_only(args: list[str]) -> bool:
    """True when ``fd <args...>`` has no exec flag. fd allows clustering short
    options (e.g. ``-Hx`` means ``-H -x``), so exact-token matching alone would
    let a clustered exec flag slip through. Conservative by design: any
    single-dash token containing ``x`` or ``X`` is treated as mutating, not
    just the bare ``-x``/``-X``."""
    for tok in args:
        if tok in _FD_MUTATING or tok.startswith(_FD_MUTATING_PREFIXES):
            return False
        if tok.startswith("-") and not tok.startswith("--") and ("x" in tok or "X" in tok):
            return False
    return True


def _rg_is_read_only(args: list[str]) -> bool:
    """True when ``rg <args...>`` has no preprocessor/child-executable flag.
    rg has no short form for ``--pre``/``--pre-glob``/``--hostname-bin``, so
    exact/prefix token matching suffices."""
    return not any(tok in _RG_MUTATING or tok.startswith(_RG_MUTATING_PREFIXES) for tok in args)


def is_read_only(command: str) -> bool:
    """True when ``command`` is a single read-only command with no shell
    metacharacters. Conservative by design (see module docstring): chaining,
    substitution, redirection, or an unknown program is treated as not
    read-only."""
    cmd = (command or "").strip()
    if not cmd or _UNSAFE.search(cmd):
        return False
    # _UNSAFE must run on the raw string first: shlex would consume the
    # backslashes/quoting around a metacharacter (e.g. `\;`, `'|'`) before the
    # regex ever sees it. Only after that gate do we unquote, because the bash
    # tool executes via a real shell, which strips quotes before exec — a
    # classifier that tokenizes with plain ``.split()`` sees ``'-x'`` as one
    # opaque token and misses that the shell hands the program a bare ``-x``.
    # Every per-flag screen below (fd's -x, rg's --pre, git branch's flags)
    # would otherwise be bypassable just by quoting the flag.
    try:
        parts = shlex.split(cmd)
    except ValueError:
        # Unbalanced quotes etc. — the shell would also choke on this, but we
        # can't prove what it resolves to, so deny rather than guess.
        return False
    if not parts:
        return False
    program = parts[0]
    if program == "git":
        return _git_is_read_only(parts[1:])
    if program == "find":
        return not any(tok in _FIND_MUTATING for tok in parts[1:])
    if program == "fd":
        return _fd_is_read_only(parts[1:])
    if program == "rg":
        return _rg_is_read_only(parts[1:])
    return program in _ALLOWED_PROGRAMS
