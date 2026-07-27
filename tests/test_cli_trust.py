import io


def test_trust_grant_then_status(tmp_path, capsys, monkeypatch):
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    monkeypatch.delenv("MARIM_TRUST_PROJECT_HOOKS", raising=False)
    (tmp_path / ".marim" / "skills" / "s").mkdir(parents=True)
    (tmp_path / ".marim" / "skills" / "s" / "SKILL.md").write_text(
        "---\nname: s\ndescription: x\n---\n"
    )
    from marim_harness.interfaces.cli.trust_cmd import run

    run(["grant", str(tmp_path)])
    from marim_harness.trust import stored_decision

    assert stored_decision(tmp_path).trusted is True
    run(["status", str(tmp_path)])
    out = capsys.readouterr().out
    assert "trusted" in out and "skills: 1" in out


def test_trust_revoke(tmp_path, capsys, monkeypatch):
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    monkeypatch.delenv("MARIM_TRUST_PROJECT_HOOKS", raising=False)
    from marim_harness.interfaces.cli.trust_cmd import run

    run(["grant", str(tmp_path)])
    run(["revoke", str(tmp_path)])
    from marim_harness.trust import stored_decision

    assert stored_decision(tmp_path).trusted is False


def test_trust_default_action_is_status(tmp_path, monkeypatch):
    """No args at all -> action defaults to `status`, workspace defaults to cwd."""
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    monkeypatch.delenv("MARIM_TRUST_PROJECT_HOOKS", raising=False)
    monkeypatch.chdir(tmp_path)
    from marim_harness.interfaces.cli.trust_cmd import main

    out = io.StringIO()
    code = main([], out=out)
    assert code == 0
    assert "untrusted" in out.getvalue()


def test_trust_bad_directory_returns_2(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    monkeypatch.delenv("MARIM_TRUST_PROJECT_HOOKS", raising=False)
    from marim_harness.interfaces.cli.trust_cmd import main

    err = io.StringIO()
    code = main(["status", str(tmp_path / "nope")], err=err)
    assert code == 2
    assert "not a directory" in err.getvalue()


def test_trust_env_override_noted_in_status(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    monkeypatch.setenv("MARIM_TRUST_PROJECT_HOOKS", "1")
    from marim_harness.interfaces.cli.trust_cmd import main

    out = io.StringIO()
    code = main(["status", str(tmp_path)], out=out)
    assert code == 0
    text = out.getvalue()
    assert "trusted" in text
    assert "MARIM_TRUST_PROJECT_HOOKS is set" in text
