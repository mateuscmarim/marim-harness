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
    ],
)
def test_mutating_or_unknown_commands_denied(command):
    assert is_read_only(command) is False
