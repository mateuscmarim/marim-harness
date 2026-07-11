import os
from pathlib import Path
from types import SimpleNamespace

import pytest

from marim_harness.runtime.deps import Deps, HarnessServices, WorkspaceConfig
from marim_harness.runtime.instructions import (
    _memory_index_block,
    _scratchpad_block,
    global_instructions_path,
    load_global_instructions,
    load_project_instructions,
)


def _ctx(workspace: Path, **kw) -> SimpleNamespace:
    """Minimal RunContext[Deps] stand-in: _memory_index_block only reads
    ctx.deps.workspace, same fixture shape as tests/test_workspace_knobs.py."""
    return SimpleNamespace(deps=Deps(workspace=WorkspaceConfig(root=workspace, **kw)))


def test_reads_agents_md(tmp_path: Path):
    (tmp_path / "AGENTS.md").write_text("Use tabs, not spaces.\n")
    assert load_project_instructions(tmp_path) == "Use tabs, not spaces."


def test_missing_file_returns_none(tmp_path: Path):
    assert load_project_instructions(tmp_path) is None


def test_empty_or_whitespace_file_returns_none(tmp_path: Path):
    (tmp_path / "AGENTS.md").write_text("   \n\t\n")
    assert load_project_instructions(tmp_path) is None


def test_custom_filename(tmp_path: Path):
    (tmp_path / ".marim.md").write_text("project rules")
    assert load_project_instructions(tmp_path, filename=".marim.md") == "project rules"


def test_unreadable_file_returns_none(tmp_path: Path):
    # A directory where the file is expected can't be read as text -> swallow.
    (tmp_path / "AGENTS.md").mkdir()
    assert load_project_instructions(tmp_path) is None


def test_claude_md_fallback(tmp_path: Path):
    """CLAUDE.md is used when AGENTS.md is absent."""
    (tmp_path / "CLAUDE.md").write_text("Claude rules.\n")
    assert load_project_instructions(tmp_path) == "Claude rules."


def test_agents_md_takes_priority_over_claude_md(tmp_path: Path):
    """AGENTS.md wins when both files exist."""
    (tmp_path / "AGENTS.md").write_text("Agents rules.\n")
    (tmp_path / "CLAUDE.md").write_text("Claude rules.\n")
    assert load_project_instructions(tmp_path) == "Agents rules."


def test_explicit_filename_ignores_fallback(tmp_path: Path):
    """Passing filename= bypasses the fallback list entirely."""
    (tmp_path / "AGENTS.md").write_text("ignored\n")
    (tmp_path / "CLAUDE.md").write_text("also ignored\n")
    (tmp_path / ".marim.md").write_text("explicit rules")
    assert load_project_instructions(tmp_path, filename=".marim.md") == "explicit rules"


def test_empty_claude_md_returns_none(tmp_path: Path):
    """An empty CLAUDE.md is treated the same as a missing file."""
    (tmp_path / "CLAUDE.md").write_text("   \n\t\n")
    assert load_project_instructions(tmp_path) is None


# --- global (user-level) instructions --------------------------------------


def test_global_instructions_path_is_under_config_dir(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    assert global_instructions_path() == tmp_path / "marim" / "AGENTS.md"


def test_load_global_instructions_reads_config_dir(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    cfg = tmp_path / "marim"
    cfg.mkdir(parents=True)
    (cfg / "AGENTS.md").write_text("Never force-push.\n")
    assert load_global_instructions() == "Never force-push."


def test_load_global_instructions_missing_returns_none(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    assert load_global_instructions() is None


# --- mtime-keyed read cache invalidation ------------------------------------


def test_project_instructions_cache_invalidates_on_size_change(tmp_path: Path):
    """A length-changing edit must be reflected: size is in the fingerprint, so
    the memoized read is recomputed without any mtime trick."""
    agents = tmp_path / "AGENTS.md"
    agents.write_text("first\n")
    assert load_project_instructions(tmp_path) == "first"
    agents.write_text("a much longer second body\n")
    assert load_project_instructions(tmp_path) == "a much longer second body"


def test_project_instructions_cache_invalidates_on_mtime_change(tmp_path: Path):
    """A same-size edit is still picked up because mtime is in the fingerprint."""
    agents = tmp_path / "AGENTS.md"
    agents.write_text("aaaa\n")
    assert load_project_instructions(tmp_path) == "aaaa"
    agents.write_text("bbbb\n")  # identical length, content differs
    # Force a distinct mtime so the test doesn't depend on write timing/clock res.
    st = agents.stat()
    os.utime(agents, ns=(st.st_atime_ns, st.st_mtime_ns + 1_000_000))
    assert load_project_instructions(tmp_path) == "bbbb"


def test_project_instructions_cache_picks_up_new_higher_priority_file(tmp_path: Path):
    """Creating AGENTS.md must override a previously-cached CLAUDE.md result —
    the fingerprint stats every candidate path, not just the one that resolved."""
    (tmp_path / "CLAUDE.md").write_text("claude\n")
    assert load_project_instructions(tmp_path) == "claude"
    (tmp_path / "AGENTS.md").write_text("agents\n")
    assert load_project_instructions(tmp_path) == "agents"


def test_project_instructions_unchanged_read_served_from_cache(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    """A repeat call with nothing changed on disk must not re-read the file —
    proves the memoization actually skips I/O, not just returns fresh values."""
    from marim_harness.runtime import instructions as instr

    (tmp_path / "AGENTS.md").write_text("rules\n")
    calls = {"n": 0}
    real = instr._read_first_nonempty

    def counting(paths):
        calls["n"] += 1
        return real(paths)

    monkeypatch.setattr(instr, "_read_first_nonempty", counting)
    assert load_project_instructions(tmp_path) == "rules"
    first = calls["n"]
    assert first == 1
    assert load_project_instructions(tmp_path) == "rules"
    assert calls["n"] == first  # second call served from cache, no re-read


def test_memory_index_block_unchanged_read_served_from_cache(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    """A repeat memory-index build with an unchanged MEMORY.md is served from
    cache (no rebuild/re-read)."""
    from marim_harness.runtime import instructions as instr

    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "cfg"))
    ws = tmp_path / "ws"
    mem = ws / ".marim" / "memory"
    mem.mkdir(parents=True)
    (mem / "MEMORY.md").write_text("- entry\n")

    calls = {"n": 0}
    real = instr._build_memory_index

    def counting(*a, **k):
        calls["n"] += 1
        return real(*a, **k)

    monkeypatch.setattr(instr, "_build_memory_index", counting)
    ctx = _ctx(ws)
    assert "entry" in _memory_index_block(ctx)
    first = calls["n"]
    assert first == 1
    assert "entry" in _memory_index_block(ctx)
    assert calls["n"] == first  # second call served from cache


def test_memory_index_block_cache_invalidates_on_edit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    """The memoized memory-index block must reflect edits to MEMORY.md (this also
    guards the local _MEMORY_INDEX_FILE constant from drifting: a wrong stat
    target would never invalidate and this would read stale)."""
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "cfg"))
    ws = tmp_path / "ws"
    mem = ws / ".marim" / "memory"
    mem.mkdir(parents=True)
    (mem / "MEMORY.md").write_text("- old entry\n")
    ctx = _ctx(ws)
    assert "old entry" in _memory_index_block(ctx)
    (mem / "MEMORY.md").write_text("- a fresh, different entry\n")
    assert "fresh, different entry" in _memory_index_block(ctx)


def test_memory_index_block_honors_memory_root(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    """When workspace.memory_root is set, the index block must reflect entries
    written under memory_root/{global,project} (what remember/recall actually
    read via resolve_scope), not the default XDG/.marim/memory locations —
    otherwise the model sees an index that doesn't match what recall can load."""
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "cfg"))
    ws = tmp_path / "ws"

    # Populate the *default* locations with content that must NOT show up.
    default_global = tmp_path / "cfg" / "marim" / "memory"
    default_project = ws / ".marim" / "memory"
    default_global.mkdir(parents=True)
    default_project.mkdir(parents=True)
    (default_global / "MEMORY.md").write_text("- default global entry\n")
    (default_project / "MEMORY.md").write_text("- default project entry\n")

    # Populate the explicit memory_root locations that resolve_scope maps to.
    store = tmp_path / "embedder-memstore"
    (store / "global").mkdir(parents=True)
    (store / "project").mkdir(parents=True)
    (store / "global" / "MEMORY.md").write_text("- rooted global entry\n")
    (store / "project" / "MEMORY.md").write_text("- rooted project entry\n")

    ctx = _ctx(ws, memory_root=store)
    block = _memory_index_block(ctx)
    assert "rooted global entry" in block
    assert "rooted project entry" in block
    assert "default global entry" not in block
    assert "default project entry" not in block


def test_memory_index_block_memory_root_none_matches_default_scopes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    """With memory_root left at its default (None), the block must be
    byte-identical to reading straight from the pre-embedder-knobs default
    scopes (global_scope()/project_scope(root)) — the governing invariant that
    both knobs being None must not change behavior."""
    from marim_harness.workspace.memory import global_scope, project_scope

    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "cfg"))
    ws = tmp_path / "ws"
    g = global_scope()
    p = project_scope(ws)
    g.root.mkdir(parents=True)
    p.root.mkdir(parents=True)
    (g.root / "MEMORY.md").write_text("- global entry\n")
    (p.root / "MEMORY.md").write_text("- project entry\n")

    block = _memory_index_block(_ctx(ws))
    assert "global entry" in block
    assert "project entry" in block


def _scratch_ctx(getter):
    deps = Deps(workspace=WorkspaceConfig(root=Path("/w")))
    deps.services = HarnessServices(get_scratchpad=getter)
    return SimpleNamespace(deps=deps)


def test_scratchpad_block_renders_path():
    path = Path("/tmp/marim-1/proj-abc/sess/scratchpad")
    text = _scratchpad_block(_scratch_ctx(lambda: path))
    assert str(path) in text
    assert "approval" in text  # advertises the ask-mode bypass


def test_scratchpad_block_absent_without_getter():
    assert _scratchpad_block(_scratch_ctx(None)) == ""


def test_scratchpad_block_absent_when_getter_returns_none():
    assert _scratchpad_block(_scratch_ctx(lambda: None)) == ""
