import io
import json
from pathlib import Path

from marim_harness.interfaces.cli import plugin as plugin_cmd


def _make_source(src: Path, name: str, *, with_hooks: bool = False, with_lsp: bool = False):
    (src / ".marim-plugin").mkdir(parents=True, exist_ok=True)
    manifest: dict[str, object] = {"name": name, "version": "1.0.0", "description": "d"}
    if with_lsp:
        # A declarative third-party lsp block launches ``command`` on connect —
        # executable surface in the same risk class as hooks/MCP.
        manifest["lsp"] = {"language": "go", "extensions": [".go"], "command": "gopls"}
    (src / ".marim-plugin" / "plugin.json").write_text(
        json.dumps(manifest), encoding="utf-8"
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


def test_install_lsp_only_prompts_and_mentions_lsp(tmp_path, monkeypatch):
    """An lsp-only plugin (no hooks/MCP) launches a process on connect, so it
    must prompt for trust AND the prompt summary must disclose the LSP surface —
    otherwise the user is asked to consent to something the message hides."""
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "cfg"))
    monkeypatch.chdir(tmp_path)
    src = tmp_path / "src"
    _make_source(src, "lsponly", with_lsp=True)

    prompted = {}

    def _decline(_p):
        prompted["asked"] = True
        return "n"

    code, out, err = _run(["install", str(src)], input_fn=_decline)
    assert code == 0, err
    assert prompted.get("asked") is True  # the gate fired
    assert "1 LSP servers" in out  # summary discloses the LSP surface
    # Declined → not trusted.
    code, out, err = _run(["list", "--json"])
    rec = {p["name"]: p for p in json.loads(out)}["lsponly"]
    assert rec["trusted"] is False


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


def test_workspace_flag_targets_project_scope_off_cwd(tmp_path, monkeypatch):
    """-C/--workspace picks the workspace root for project-scoped plugins instead
    of cwd: a project install under one dir is visible via -C from elsewhere."""
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "cfg"))
    proj = tmp_path / "proj"
    proj.mkdir()
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    monkeypatch.chdir(elsewhere)  # cwd is NOT the project

    src = tmp_path / "src"
    _make_source(src, "demo")
    code, out, err = _run(["-C", str(proj), "install", str(src), "--scope", "project"])
    assert code == 0, err
    # Project plugin lives under proj/.marim, not cwd.
    assert (proj / ".marim" / "plugins" / "demo").exists()

    # From cwd alone (no -C) the project plugin is invisible…
    code, out, err = _run(["list"])
    assert "demo" not in out
    # …but -C surfaces it.
    code, out, err = _run(["-C", str(proj), "list"])
    assert "demo" in out


def test_workspace_flag_rejects_missing_dir(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "cfg"))
    monkeypatch.chdir(tmp_path)
    code, out, err = _run(["-C", str(tmp_path / "nope"), "list"])
    assert code == 2
    assert "not a directory" in err


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
