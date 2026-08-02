import sys
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


def test_bad_from_exits_two(tmp_path, capsys):
    ws = tmp_path / "ws"
    ws.mkdir()
    missing = tmp_path / "nope"
    from marim_harness.interfaces.cli.import_cmd import run

    assert run(["claude", str(ws), "--from", str(missing)]) == 2
    assert "not a directory" in capsys.readouterr().err


def test_router_routes_the_import_keyword():
    from marim_harness.interfaces.cli import router

    assert "import" in router._MANAGEMENT
    assert router._MODULE_NAMES["import"] == "import_cmd"


def test_nothing_to_import_on_an_empty_source(tmp_path, capsys, monkeypatch):
    """The spec's clean-exit path: a source dir that resolves but holds no
    memories is exit 0 with a "nothing to import" line, not an error."""
    ws = tmp_path / "ws"
    ws.mkdir()
    cfg = tmp_path / "cc"
    _claude_store(cfg, ws, [])
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(cfg))
    from marim_harness.interfaces.cli.import_cmd import run

    assert run(["claude", str(ws), "--apply"]) == 0
    out = capsys.readouterr().out
    assert "nothing to import." in out
    assert not (ws / ".marim" / "memory").exists()


def test_source_problems_reach_stderr(tmp_path, capsys, monkeypatch):
    """One corrupt file must not cost the user the rest of the store: it is
    reported on stderr and the good memory still imports."""
    ws = tmp_path / "ws"
    ws.mkdir()
    cfg = tmp_path / "cc"
    src = _claude_store(cfg, ws, [("alpha", "Alpha Fact", "A body")])
    (src / "junk.md").write_text("no frontmatter here\n", encoding="utf-8")
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(cfg))
    from marim_harness.interfaces.cli.import_cmd import run

    assert run(["claude", str(ws), "--apply"]) == 0
    captured = capsys.readouterr()
    assert "source problem" in captured.err and "junk.md" in captured.err
    assert (ws / ".marim" / "memory" / "alpha.md").exists()


def _git(repo: Path, *args: str) -> None:
    import subprocess

    subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True)


def test_privacy_warning_fires_in_a_repo_that_does_not_ignore_dot_marim(
    tmp_path, capsys, monkeypatch
):
    """The feature's one user-facing privacy surface: a personal Claude memory
    store landing in committable space."""
    ws = tmp_path / "ws"
    ws.mkdir()
    _git(ws, "init", "-q")
    cfg = tmp_path / "cc"
    _claude_store(cfg, ws, [("alpha", "Alpha Fact", "A body")])
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(cfg))
    from marim_harness.interfaces.cli.import_cmd import run

    assert run(["claude", str(ws), "--apply"]) == 0
    err = capsys.readouterr().err
    assert "not gitignored" in err and "committable" in err


def test_privacy_warning_is_silent_when_dot_marim_is_gitignored(tmp_path, capsys, monkeypatch):
    """`git check-ignore -q` exits 0 for an ignored path. Discriminating that
    from 1 (not ignored) is the whole point of `_repo_tracks_target`; a
    `returncode != 0` simplification would invert this case."""
    ws = tmp_path / "ws"
    ws.mkdir()
    _git(ws, "init", "-q")
    (ws / ".gitignore").write_text(".marim/\n", encoding="utf-8")
    cfg = tmp_path / "cc"
    _claude_store(cfg, ws, [("alpha", "Alpha Fact", "A body")])
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(cfg))
    from marim_harness.interfaces.cli.import_cmd import run

    assert run(["claude", str(ws), "--apply"]) == 0
    assert "not gitignored" not in capsys.readouterr().err


def test_privacy_warning_is_silent_outside_a_git_repo(tmp_path, capsys, monkeypatch):
    ws = tmp_path / "ws"
    ws.mkdir()
    cfg = tmp_path / "cc"
    _claude_store(cfg, ws, [("alpha", "Alpha Fact", "A body")])
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(cfg))
    from marim_harness.interfaces.cli.import_cmd import run

    assert run(["claude", str(ws), "--apply"]) == 0
    assert "not gitignored" not in capsys.readouterr().err


def test_privacy_warning_is_not_printed_on_a_dry_run(tmp_path, capsys, monkeypatch):
    """No write, no warning — and `_repo_tracks_target` (the only subprocess in
    the command) must stay behind the dry-run early return."""
    ws = tmp_path / "ws"
    ws.mkdir()
    _git(ws, "init", "-q")
    cfg = tmp_path / "cc"
    _claude_store(cfg, ws, [("alpha", "Alpha Fact", "A body")])
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(cfg))
    from marim_harness.interfaces.cli.import_cmd import run

    assert run(["claude", str(ws)]) == 0
    assert "not gitignored" not in capsys.readouterr().err


def test_repo_tracks_target_is_false_when_git_cannot_answer(tmp_path, monkeypatch):
    """A missing git binary, or any return code other than 1, means "we cannot
    tell" — and a warning printed when we cannot tell is worse than silence."""
    import subprocess

    from marim_harness.interfaces.cli import import_cmd

    ws = tmp_path / "ws"
    (ws / ".git").mkdir(parents=True)

    def boom(*a, **k):
        raise OSError("no git here")

    monkeypatch.setattr(import_cmd.subprocess, "run", boom)
    assert import_cmd._repo_tracks_target(ws) is False

    monkeypatch.setattr(
        import_cmd.subprocess, "run",
        lambda *a, **k: subprocess.CompletedProcess(a[0] if a else [], 128, b"", b""),
    )
    assert import_cmd._repo_tracks_target(ws) is False


def test_resolve_source_reports_a_from_path_that_vanished(tmp_path, capsys):
    """`main` pre-validates `--from`, so this branch is a TOCTOU-only guard and
    is only reachable by calling the helper directly — which is what keeps it a
    total function rather than one correct only under a caller's precondition."""
    from marim_harness.interfaces.cli import import_cmd

    got = import_cmd._resolve_source(str(tmp_path / "gone"), tmp_path, err=sys.stderr)
    assert got is None
    assert "not a directory" in capsys.readouterr().err


def _marim_memory(ws: Path, *, slug: str, title: str, body: str):
    from marim_harness.workspace import memory

    scope = memory.project_scope(ws)
    memory.save_memory(
        scope, name=slug, description="marim's own note", mem_type="project",
        body=body, title=title,
    )
    return scope


def test_apply_does_not_clobber_a_marim_memory_on_a_normalized_title(
    tmp_path, capsys, monkeypatch
):
    """The C1 repro, at the CLI level: no --force, exit 0 is only honest if the
    marim-authored memory is still on disk afterward."""
    ws = tmp_path / "ws"
    ws.mkdir()
    scope = _marim_memory(ws, slug="marim-deploy", title="Deploy notes", body="MARIM ORIGINAL")
    cfg = tmp_path / "cc"
    _claude_store(cfg, ws, [("alpha", "Deploy (notes)", "CLAUDE BODY")])
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(cfg))
    from marim_harness.interfaces.cli.import_cmd import run

    assert run(["claude", str(ws), "--apply"]) == 0
    out = capsys.readouterr().out
    assert "skip" in out and "0 imported, 1 skipped" in out
    kept = (scope.root / "marim-deploy.md").read_text(encoding="utf-8")
    assert "MARIM ORIGINAL" in kept and "CLAUDE BODY" not in kept
    assert not (scope.root / "alpha.md").exists()


def test_force_report_names_the_file_that_changes(tmp_path, capsys, monkeypatch):
    """M2: the plan line has to show the redirect, because `alpha.md` is never
    created — `marim-deploy.md` is what the run overwrites."""
    ws = tmp_path / "ws"
    ws.mkdir()
    scope = _marim_memory(ws, slug="marim-deploy", title="Deploy notes", body="MARIM ORIGINAL")
    cfg = tmp_path / "cc"
    _claude_store(cfg, ws, [("alpha", "Deploy notes", "CLAUDE BODY")])
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(cfg))
    from marim_harness.interfaces.cli.import_cmd import run

    assert run(["claude", str(ws), "--apply", "--force"]) == 0
    out = capsys.readouterr().out
    assert "alpha → marim-deploy" in out
    assert "CLAUDE BODY" in (scope.root / "marim-deploy.md").read_text(encoding="utf-8")
    assert not (scope.root / "alpha.md").exists()
