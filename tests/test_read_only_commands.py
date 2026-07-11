import pytest

from marim_harness.read_only_commands import is_read_only


@pytest.mark.parametrize(
    "command",
    [
        "git status",
        "git log --oneline -20",
        "git diff HEAD~1",
        "ls -la src/",
        "cat pyproject.toml",
        "rg -n 'def foo' src",
        "grep -rn TODO src",
        "find . -name '*.py'",
        "tree -L 2",
        "head -50 README.md",
        "wc -l src/marim_harness/tasks.py",
        # Listing forms of branch/tag/remote stay read-only.
        "git branch",
        "git branch -a",
        "git branch --show-current",
        "git tag",
        "git tag --list",
        "git remote",
        "git remote -v",
        "git remote show origin",
        "git remote get-url origin",
        # Plain fd/rg usage, with no exec/preprocessor flags, stays read-only.
        "fd pattern",
        "fd -e py pattern",
        "rg -n pattern src",
        "rg --heading x",
        # Quoting a benign value/pattern must not flip a command to denied —
        # shlex unquotes these to ordinary arguments with no special meaning.
        "grep 'some pattern' file",
        "git log --grep='fix bug'",
        # `git branch '-a'` used to be a false denial under plain .split()
        # (the token was the literal 4 chars `'-a'`, which never matched
        # `_BRANCH_SAFE_FLAGS`'s `-a`). shlex unquotes it to the real flag,
        # so it now correctly allows through like unquoted `git branch -a`.
        "git branch '-a'",
    ],
)
def test_read_only_commands_allowed(command):
    assert is_read_only(command) is True


@pytest.mark.parametrize(
    "command",
    [
        "rm -rf build",
        "git push origin main",
        "git commit -m x",
        "echo hi > file.txt",
        "cat a >> b",
        "ls && rm -rf x",
        "ls; rm x",
        "ls | tee out.txt",
        "python -c 'open(\"x\",\"w\")'",
        "$(echo rm) -rf x",
        "pip install requests",
        "",
        "   ",
        "mv a b",
        "find . -delete",
        "find . -name x -exec rm {} +",
        "env rm -rf x",
        "find . -execdir rm {} +",
        # Mutating forms of the "listing" git subcommands.
        "git branch new-feature",
        "git branch -D main",
        "git branch -m old new",
        "git tag v1.0",
        "git tag -d v1.0",
        "git tag -a v1 -m msg",
        "git remote add evil https://example.com/x.git",
        "git remote set-url origin https://example.com/x.git",
        "git remote remove origin",
        "git remote prune origin",
        "git remote update",
        # --output/-o make diff-family commands write a file.
        "git diff --output=/tmp/x",
        "git diff --output /tmp/x",
        "git log -o /tmp/x",
        # fd -x/-X/--exec/--exec-batch run an arbitrary command per match, with
        # no shell metacharacters for _UNSAFE to catch.
        "fd -x rm",
        "fd --exec rm",
        "fd -X trash",
        "fd --exec-batch rm",
        "fd --exec=rm",
        "fd --exec-batch=trash pattern",
        # Clustered short flags: fd allows `-Hx` as `-H -x`.
        "fd -Hx rm",
        # rg --pre/--pre-glob run an arbitrary preprocessor command per file.
        "rg --pre cat pattern",
        "rg --pre=cat pattern",
        "rg --pre-glob '*.md' --pre x",
        # rg >=14 --hostname-bin runs an arbitrary executable, same class as
        # --pre.
        "rg --hostname-bin=./payload p",
        "rg --hostname-bin ./payload p",
        # Quoting a flag must not bypass the fd/rg exec-flag screens: the bash
        # tool runs through a real shell, which strips the quotes before exec,
        # so the classifier must see the post-unquoting token.
        "fd '-x' rm",
        'fd "-x" rm',
        "rg '--pre' cat p",
        'rg "--pre=cat" p',
        # Unbalanced quoting: shlex.split raises ValueError, and we can't
        # prove what the real shell would do with it, so deny.
        "fd 'unclosed",
    ],
)
def test_mutating_or_unknown_commands_denied(command):
    assert is_read_only(command) is False
