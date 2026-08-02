from pathlib import Path


def _claude_store(config_dir: Path, workspace: Path, memories):
    """Build a fake Claude memory store for `workspace` under `config_dir`."""
    from marim_harness.workspace.claude_import import claude_memory_dir

    src = claude_memory_dir(workspace, config_dir=config_dir)
    src.mkdir(parents=True)
    lines = []
    for slug, title, body in memories:
        (src / f"{slug}.md").write_text(
            f"---\nname: {slug}\ndescription: about {slug}\n"
            f"metadata:\n  type: project\n---\n\n{body}\n",
            encoding="utf-8",
        )
        lines.append(f"- [{title}]({slug}.md) — hook for {slug}")
    (src / "MEMORY.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
    return src


def test_dry_run_lists_but_writes_nothing(tmp_path, capsys, monkeypatch):
    ws = tmp_path / "ws"
    ws.mkdir()
    cfg = tmp_path / "cc"
    _claude_store(cfg, ws, [("alpha", "Alpha Fact", "A body")])
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(cfg))
    from marim_harness.interfaces.cli.import_cmd import run

    assert run(["claude", str(ws)]) == 0
    out = capsys.readouterr().out
    assert "import" in out and "alpha" in out
    assert "Dry run" in out
    assert not (ws / ".marim" / "memory").exists()


def test_apply_writes_the_memories(tmp_path, capsys, monkeypatch):
    ws = tmp_path / "ws"
    ws.mkdir()
    cfg = tmp_path / "cc"
    _claude_store(cfg, ws, [("alpha", "Alpha Fact", "A body")])
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(cfg))
    from marim_harness.interfaces.cli.import_cmd import run

    assert run(["claude", str(ws), "--apply"]) == 0
    target = ws / ".marim" / "memory"
    assert "A body" in (target / "alpha.md").read_text(encoding="utf-8")
    assert "- [Alpha Fact](alpha.md)" in (target / "MEMORY.md").read_text(encoding="utf-8")
    assert "Dry run" not in capsys.readouterr().out


def test_second_apply_skips_without_force(tmp_path, capsys, monkeypatch):
    ws = tmp_path / "ws"
    ws.mkdir()
    cfg = tmp_path / "cc"
    _claude_store(cfg, ws, [("alpha", "Alpha Fact", "A body")])
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(cfg))
    from marim_harness.interfaces.cli.import_cmd import run

    run(["claude", str(ws), "--apply"])
    capsys.readouterr()
    assert run(["claude", str(ws), "--apply"]) == 0
    out = capsys.readouterr().out
    assert "skip" in out
    assert "1 skipped" in out


def test_force_overwrites(tmp_path, capsys, monkeypatch):
    """Import once, rewrite the source memory's body, import again with
    --force: the new body must win."""
    ws = tmp_path / "ws"
    ws.mkdir()
    cfg = tmp_path / "cc"
    _claude_store(cfg, ws, [("alpha", "Alpha Fact", "original")])
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(cfg))
    from marim_harness.interfaces.cli.import_cmd import run
    from marim_harness.workspace.claude_import import claude_memory_dir

    run(["claude", str(ws), "--apply"])
    (claude_memory_dir(ws, config_dir=cfg) / "alpha.md").write_text(
        "---\nname: alpha\ndescription: about alpha\n"
        "metadata:\n  type: project\n---\n\nrewritten\n",
        encoding="utf-8",
    )
    capsys.readouterr()
    assert run(["claude", str(ws), "--apply", "--force"]) == 0
    body = (ws / ".marim" / "memory" / "alpha.md").read_text(encoding="utf-8")
    assert "rewritten" in body and "original" not in body


def test_missing_source_lists_candidates_and_exits_one(tmp_path, capsys, monkeypatch):
    ws = tmp_path / "ws"
    ws.mkdir()
    other = tmp_path / "other"
    other.mkdir()
    cfg = tmp_path / "cc"
    _claude_store(cfg, other, [("beta", "Beta", "B body")])
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(cfg))
    from marim_harness.interfaces.cli.import_cmd import run

    assert run(["claude", str(ws)]) == 1
    err = capsys.readouterr().err
    assert "--from" in err
    assert "other" in err


def test_from_accepts_a_project_dir_or_a_memory_dir(tmp_path, capsys, monkeypatch):
    ws = tmp_path / "ws"
    ws.mkdir()
    cfg = tmp_path / "cc"
    other = tmp_path / "other"
    other.mkdir()
    src = _claude_store(cfg, other, [("beta", "Beta", "B body")])
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(cfg))
    from marim_harness.interfaces.cli.import_cmd import run

    assert run(["claude", str(ws), "--from", str(src)]) == 0
    assert "beta" in capsys.readouterr().out
    assert run(["claude", str(ws), "--from", str(src.parent)]) == 0
    assert "beta" in capsys.readouterr().out


def test_bad_workspace_exits_two(tmp_path, capsys):
    from marim_harness.interfaces.cli.import_cmd import run

    assert run(["claude", str(tmp_path / "nope")]) == 2
    assert "not a directory" in capsys.readouterr().err


def test_failed_write_exits_one(tmp_path, capsys, monkeypatch):
    ws = tmp_path / "ws"
    ws.mkdir()
    cfg = tmp_path / "cc"
    _claude_store(cfg, ws, [("alpha", "Alpha Fact", "A body")])
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(cfg))
    from marim_harness.workspace import claude_import

    monkeypatch.setattr(claude_import, "save_memory", lambda *a, **k: None)
    from marim_harness.interfaces.cli.import_cmd import run

    assert run(["claude", str(ws), "--apply"]) == 1
    assert "alpha" in capsys.readouterr().err


def test_router_routes_the_import_keyword():
    from marim_harness.interfaces.cli import router

    assert "import" in router._MANAGEMENT
    assert router._MODULE_NAMES["import"] == "import_cmd"
