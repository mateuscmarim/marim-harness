import io
import json
from pathlib import Path

from marim_harness.interfaces.cli import plugin as plugin_cmd


def _make_source(src: Path, name: str, *, with_hooks: bool = False):
    (src / ".marim-plugin").mkdir(parents=True, exist_ok=True)
    (src / ".marim-plugin" / "plugin.json").write_text(
        json.dumps({"name": name, "version": "1.0.0", "description": "d"}), encoding="utf-8"
    )
    sk = src / "skills" / "demo"
    sk.mkdir(parents=True, exist_ok=True)
    (sk / "SKILL.md").write_text("---\nname: demo\ndescription: d\n---\nx", encoding="utf-8")
    if with_hooks:
        (src / "hooks").mkdir(parents=True, exist_ok=True)
        (src / "hooks" / "hooks.json").write_text(
            json.dumps({"hooks": {"Stop": [{"type": "command", "command": "echo"}]}}),
            encoding="utf-8",
        )


def _run(argv, **kw):
    out, err = io.StringIO(), io.StringIO()
    code = plugin_cmd.main(argv, out=out, err=err, now_fn=lambda: "T", **kw)
    return code, out.getvalue(), err.getvalue()


def test_install_inert_and_list(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "cfg"))
    monkeypatch.chdir(tmp_path)
    src = tmp_path / "src"
    _make_source(src, "demo")
    code, out, err = _run(["install", str(src)])
    assert code == 0, err
    code, out, err = _run(["list"])
    assert "demo" in out
    assert "enabled" in out


def test_install_executable_prompts_for_trust(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "cfg"))
    monkeypatch.chdir(tmp_path)
    src = tmp_path / "src"
    _make_source(src, "exec", with_hooks=True)
    # Decline trust at the prompt.
    code, out, err = _run(["install", str(src)], input_fn=lambda _p: "n")
    assert code == 0
    code, out, err = _run(["list", "--json"])
    rec = {p["name"]: p for p in json.loads(out)}["exec"]
    assert rec["trusted"] is False


def test_install_trust_flag_headless(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "cfg"))
    monkeypatch.chdir(tmp_path)
    src = tmp_path / "src"
    _make_source(src, "exec", with_hooks=True)

    def _no_input(_p):
        raise AssertionError("must not prompt when --trust is given")

    code, out, err = _run(["install", str(src), "--trust"], input_fn=_no_input)
    assert code == 0
    code, out, err = _run(["list", "--json"])
    rec = {p["name"]: p for p in json.loads(out)}["exec"]
    assert rec["trusted"] is True


def test_enable_disable_remove(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "cfg"))
    monkeypatch.chdir(tmp_path)
    src = tmp_path / "src"
    _make_source(src, "demo")
    _run(["install", str(src)])
    assert _run(["disable", "demo"])[0] == 0
    assert _run(["enable", "demo"])[0] == 0
    assert _run(["remove", "demo"])[0] == 0
    code, out, err = _run(["info", "demo"])
    assert code != 0  # gone


def test_validate(tmp_path, monkeypatch):
    src = tmp_path / "src"
    _make_source(src, "demo")
    code, out, err = _run(["validate", str(src)])
    assert code == 0
    bad = tmp_path / "bad"
    bad.mkdir()
    assert _run(["validate", str(bad)])[0] != 0


def test_install_inert_does_not_prompt(tmp_path, monkeypatch):
    """Verify that inert (no hooks/MCP) plugins do not prompt for trust."""
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "cfg"))
    monkeypatch.chdir(tmp_path)
    src = tmp_path / "src"
    _make_source(src, "demo")

    def _must_not_prompt(_p):
        raise AssertionError("inert plugin must not prompt for trust")

    code, out, err = _run(["install", str(src)], input_fn=_must_not_prompt)
    assert code == 0, err


def test_install_executable_accept_trust(tmp_path, monkeypatch):
    """Verify that accepting the trust prompt marks an executable plugin as trusted."""
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "cfg"))
    monkeypatch.chdir(tmp_path)
    src = tmp_path / "src"
    _make_source(src, "exec", with_hooks=True)
    code, out, err = _run(["install", str(src)], input_fn=lambda _p: "yes")
    assert code == 0, err
    code, out, err = _run(["list", "--json"])
    rec = {p["name"]: p for p in json.loads(out)}["exec"]
    assert rec["trusted"] is True
