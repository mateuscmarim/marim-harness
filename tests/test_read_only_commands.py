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
    ],
)
def test_mutating_or_unknown_commands_denied(command):
    assert is_read_only(command) is False
