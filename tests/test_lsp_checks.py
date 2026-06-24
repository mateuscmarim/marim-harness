import json

import pytest

from marim_harness.lsp import checks
from marim_harness.lsp.checks import Diag, _parse_pyright, _parse_ruff, format_checks


@pytest.fixture
def anyio_backend():
    return "asyncio"


def test_parse_ruff_coded_and_syntax():
    out = json.dumps(
        [
            {
                "code": "F821",
                "message": "Undefined name `foo`",
                "location": {"row": 3, "column": 7},
            },
            {  # syntax error: ruff emits a null code
                "code": None,
                "message": "SyntaxError: unexpected EOF",
                "location": {"row": 1, "column": 1},
            },
        ]
    )
    diags = _parse_ruff(out)
    assert diags[0] == Diag(3, 7, "warning", "Undefined name `foo` (F821)", "ruff")
    assert diags[1].severity == "error"  # null code → syntax error


def test_parse_ruff_handles_garbage():
    assert _parse_ruff("not json") == []
    assert _parse_ruff("") == []


def test_parse_pyright_translates_to_1_based():
    out = json.dumps(
        {
            "generalDiagnostics": [
                {
                    "severity": "error",
                    "message": 'Argument of type "str"...\nsecond line',
                    "range": {"start": {"line": 4, "character": 2}},
                    "rule": "reportArgumentType",
                }
            ]
        }
    )
    diags = _parse_pyright(out)
    assert diags[0].line == 5 and diags[0].col == 3  # 0-based → 1-based
    assert diags[0].severity == "error"
    assert diags[0].message.endswith("(reportArgumentType)")
    assert "second line" not in diags[0].message  # only first line kept


def test_format_checks_shape_and_empty():
    assert format_checks("a.py", []) == "a.py: no diagnostics"
    out = format_checks("a.py", [Diag(2, 1, "error", "boom (F821)", "ruff")])
    assert out == "a.py:2:1: error: boom (F821) [ruff]"


def test_format_checks_truncates():
    diags = [Diag(i, 1, "warning", f"m{i}", "ruff") for i in range(60)]
    out = format_checks("a.py", diags, max_results=10)
    assert "… and 50 more" in out


@pytest.mark.anyio
async def test_python_diagnostics_merges_ruff_and_pyright(tmp_path, monkeypatch):
    """ruff + pyright results merge and sort by position; pyright runs only on a
    deep check."""
    monkeypatch.setattr(checks.shutil, "which", lambda b: f"/usr/bin/{b}")
    ruff_json = json.dumps(
        [{"code": "F401", "message": "unused import", "location": {"row": 9, "column": 1}}]
    )
    pyright_json = json.dumps(
        {
            "generalDiagnostics": [
                {
                    "severity": "error",
                    "message": "type mismatch",
                    "range": {"start": {"line": 0, "character": 0}},
                }
            ]
        }
    )

    async def fake_run(cmd, cwd, timeout):
        return pyright_json if cmd[0] in ("pyright", "basedpyright") else ruff_json

    monkeypatch.setattr(checks, "_run", fake_run)
    diags = await checks.python_diagnostics(tmp_path, "m.py", deep=True)
    assert [d.source for d in diags] == ["pyright", "ruff"]  # sorted by line (1, 9)


@pytest.mark.anyio
async def test_python_diagnostics_skips_pyright_when_absent(tmp_path, monkeypatch):
    """No pyright binary, or a non-deep check, means ruff-only — no pyright call."""
    monkeypatch.setattr(
        checks.shutil, "which", lambda b: "/usr/bin/ruff" if b == "ruff" else None
    )
    calls: list = []

    async def fake_run(cmd, cwd, timeout):
        calls.append(cmd[0])
        return "[]"

    monkeypatch.setattr(checks, "_run", fake_run)
    await checks.python_diagnostics(tmp_path, "m.py", deep=True)
    assert calls == ["ruff"]  # pyright absent → not invoked


@pytest.mark.anyio
async def test_python_diagnostics_real_ruff(tmp_path):
    """End-to-end against the real ruff binary (a hard dependency): an undefined
    name is reported. This is the actual win over jedi's syntax-only diagnostics."""
    (tmp_path / "bad.py").write_text("print(undefined_name)\n")
    diags = await checks.python_diagnostics(tmp_path, "bad.py", deep=False)
    assert any("undefined_name" in d.message and d.source == "ruff" for d in diags)
