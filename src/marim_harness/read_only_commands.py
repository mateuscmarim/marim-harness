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
from collections.abc import Callable

# Shell metacharacters that enable chaining, command substitution, or writing.
# Any of these and we refuse to call the command read-only: cheaply proving every
# chained segment is safe is not worth it, and research rarely needs them.
_UNSAFE = re.compile(r"[;&|<>`\n]|\$\(")

# Single programs that only read. ``echo``/``printf`` are read-only on their own
# (a write needs a redirection, which ``_UNSAFE`` already rejects). ``fd``,
# ``rg``, ``tree``, ``file``, ``date`` and ``hostname`` are deliberately absent —
# each has an arg-screening function in ``_PROGRAM_SCREENERS`` (below) that
# returns before this roster is ever consulted, so it fully owns that program's
# classification. Membership here is a bare, unscreened "always read-only" pass,
# reserved for programs with no mutating invocation to screen for.
_ALLOWED_PROGRAMS = frozenset(
    {
        "ls", "cat", "head", "tail", "wc", "stat", "pwd",
        "whoami", "uname", "which", "type", "grep",
        "find", "ack", "echo", "printf",
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

# ``date`` display flags that consume the following token as their value
# (a date to *print*, not to set), so that value is not itself a clock-setting
# positional and must be skipped when screening. ``--date=``/``--reference=``/
# ``--file=`` joined forms start with ``-`` and are handled by the flag branch.
_DATE_VALUE_FLAGS = frozenset({"-d", "--date", "-r", "--reference", "-f", "--file"})

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


def _find_is_read_only(args: list[str]) -> bool:
    """True when ``find <args...>`` has no primary that executes or writes.
    ``-exec ... \\;`` is already caught by _UNSAFE's ``;``; the ``+`` terminator,
    -delete, and the -fprint* family are not, so screen the tokens explicitly."""
    return not any(tok in _FIND_MUTATING for tok in args)


def _tree_is_read_only(args: list[str]) -> bool:
    """True when ``tree <args...>`` has no output-to-file flag. ``-o``/
    ``--output`` redirect tree's listing into an arbitrary file — a write with
    no shell redirection for _UNSAFE to catch (verified: ``tree -o f`` creates
    f). Every other tree flag only formats what it prints. tree can cluster
    short options, so any single-dash token containing ``o`` is treated as
    carrying ``-o`` — conservative by design (no other short flag uses ``o``)."""
    for tok in args:
        if tok in ("-o", "--output") or tok.startswith("--output="):
            return False
        if tok.startswith("-") and not tok.startswith("--") and "o" in tok:
            return False
    return True


def _file_is_read_only(args: list[str]) -> bool:
    """True when ``file <args...>`` has no compile flag. ``-C``/``--compile``
    writes a compiled magic database (``foo.mgc``) — a write with no shell
    redirection for _UNSAFE to catch. Every other thing file does only reads the
    named files (``-m`` supplies a magic file to read *with*, not write). file
    uses getopt-style clustering (``-bC``), so any single-dash token containing
    ``C`` is treated as carrying ``-C`` — conservative by design."""
    for tok in args:
        if tok == "--compile":
            return False
        if tok.startswith("-") and not tok.startswith("--") and "C" in tok:
            return False
    return True


def _date_is_read_only(args: list[str]) -> bool:
    """True when ``date <args...>`` only prints. ``-s``/``--set`` writes the
    system clock, and a bare positional (``date 010112002020``) sets it too —
    both mutate (root-gated, but a mutation is a mutation). A ``+FORMAT`` token
    and ordinary flags only affect what is printed; the value consumed by a
    display flag (``-d``/``--date`` etc.) is skipped so it is not mistaken for a
    clock-setting positional."""
    expect_value = False
    for tok in args:
        if expect_value:
            expect_value = False
            continue
        if tok in ("-s", "--set") or tok.startswith("--set="):
            return False
        if tok in _DATE_VALUE_FLAGS:
            expect_value = True
            continue
        if tok.startswith("-") or tok.startswith("+"):
            continue
        return False  # bare positional → sets the clock
    return True


def _hostname_is_read_only(args: list[str]) -> bool:
    """True when ``hostname <args...>`` only prints. A bare positional
    (``hostname evil``) sets the host name, and ``-b``/``--boot`` and
    ``-F``/``--file`` set it from a value/file — all mutations (root-gated).
    Every hostname flag that only queries starts with ``-`` and takes no
    value, so any non-flag token is a name to set."""
    for tok in args:
        if tok in ("-b", "--boot", "-F", "--file") or tok.startswith("--file="):
            return False
        if not tok.startswith("-"):
            return False  # positional → sets the host name
    return True


# Programs whose read-only classification is owned entirely by an arg-screening
# function, not by bare _ALLOWED_PROGRAMS membership: the screener inspects the
# specific flags/positionals present and returns True iff the invocation only
# reads. The dispatch in ``is_read_only`` consults this table first, so these
# programs are (and must stay) absent from _ALLOWED_PROGRAMS.
_PROGRAM_SCREENERS: dict[str, Callable[[list[str]], bool]] = {
    "git": _git_is_read_only,
    "find": _find_is_read_only,
    "fd": _fd_is_read_only,
    "rg": _rg_is_read_only,
    "tree": _tree_is_read_only,
    "file": _file_is_read_only,
    "date": _date_is_read_only,
    "hostname": _hostname_is_read_only,
}


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
    # A program with a dedicated screener (git, find, fd, rg, tree, file, date,
    # hostname) is classified solely by that screener — it replaces, and returns
    # before, any roster membership. Only programs with no mutating invocation
    # to screen for fall through to the bare _ALLOWED_PROGRAMS check.
    screener = _PROGRAM_SCREENERS.get(program)
    if screener is not None:
        return screener(parts[1:])
    return program in _ALLOWED_PROGRAMS
