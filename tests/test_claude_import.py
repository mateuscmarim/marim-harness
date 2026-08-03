from pathlib import Path

from marim_harness.workspace import claude_import


def test_project_slug_replaces_slashes_and_dots():
    """Claude names its project dirs by replacing every `/` and `.` with `-`.
    The leading `-` falls out of the leading `/`. Paths here are absolute and
    non-existent, so the internal resolve() is an identity transform."""
    slug = claude_import.claude_project_slug("/home/x/Projects/marim.dev/marim-harness")
    assert slug == "-home-x-Projects-marim-dev-marim-harness"


def test_project_slug_doubles_dash_for_dot_directories():
    """`/.local` contributes both the separator dash and the dot dash, which is
    why real Claude dirs read `-home-x--local-share-...`."""
    slug = claude_import.claude_project_slug("/home/x/.local/share/fcstudio")
    assert slug == "-home-x--local-share-fcstudio"


def test_project_slug_normalizes_trailing_slash_and_dot_segments():
    assert claude_import.claude_project_slug("/home/x/proj/") == "-home-x-proj"
    assert claude_import.claude_project_slug("/home/x/a/../proj") == "-home-x-proj"


def test_config_dir_honors_env_override(tmp_path: Path):
    got = claude_import.claude_config_dir({"CLAUDE_CONFIG_DIR": str(tmp_path / "cc")})
    assert got == tmp_path / "cc"


def test_config_dir_ignores_blank_override_and_falls_back_to_home():
    got = claude_import.claude_config_dir({"CLAUDE_CONFIG_DIR": "   "})
    assert got == Path.home() / ".claude"


def test_config_dir_defaults_to_dot_claude_in_home():
    assert claude_import.claude_config_dir({}) == Path.home() / ".claude"


def test_memory_dir_composes_projects_slug_memory(tmp_path: Path):
    got = claude_import.claude_memory_dir(
        "/home/x/Projects/app", config_dir=tmp_path / "cc"
    )
    assert got == tmp_path / "cc" / "projects" / "-home-x-Projects-app" / "memory"


_FILE = """---
name: deploy-notes
description: How the deploy works.
metadata:
  node_type: memory
  type: project
  originSessionId: abc-123
  modified: 2026-07-28T18:12:01.714Z
---

Body line one.

A separator inside the body:

---

Body after the separator.
"""


def test_parse_memory_file_extracts_fields_and_keeps_body():
    got = claude_import.parse_memory_file(_FILE, slug="deploy-notes", title="Deploy notes")
    assert got is not None
    assert got.slug == "deploy-notes"
    assert got.title == "Deploy notes"
    assert got.description == "How the deploy works."
    assert got.mem_type == "project"
    assert got.body.startswith("Body line one.")
    assert "Body after the separator." in got.body


def test_parse_memory_file_tolerates_missing_description_and_type():
    text = "---\nname: x\n---\n\nBody.\n"
    got = claude_import.parse_memory_file(text, slug="x", title="X")
    assert got is not None
    assert got.description == ""
    assert got.mem_type == "project"


def test_parse_memory_file_rejects_text_without_frontmatter():
    assert claude_import.parse_memory_file("Just a note.\n", slug="x", title="X") is None


def test_parse_memory_file_rejects_non_mapping_frontmatter():
    got = claude_import.parse_memory_file("---\n- a\n- b\n---\nBody\n", slug="x", title="X")
    assert got is None


def _write_source(root, name, description, body, mem_type="project"):
    root.mkdir(parents=True, exist_ok=True)
    (root / f"{name}.md").write_text(
        f"---\nname: {name}\ndescription: {description}\n"
        f"metadata:\n  type: {mem_type}\n---\n\n{body}\n",
        encoding="utf-8",
    )


def test_read_source_recovers_titles_from_the_index(tmp_path: Path):
    src = tmp_path / "memory"
    _write_source(src, "alpha", "First fact.", "Alpha body.")
    _write_source(src, "beta", "Second fact.", "Beta body.")
    (src / "MEMORY.md").write_text(
        "# Memory Index\n\n"
        "- [Alpha Fact](alpha.md) — first\n"
        "- [Beta Fact](beta.md) — second\n",
        encoding="utf-8",
    )
    scan = claude_import.read_source(src)
    assert [(m.slug, m.title) for m in scan.memories] == [
        ("alpha", "Alpha Fact"),
        ("beta", "Beta Fact"),
    ]
    assert scan.problems == ()


def test_read_source_does_not_misattribute_a_title_containing_a_link(tmp_path: Path):
    """`index_entries` anchors on the FIRST `](...md)` of a line. An entry whose
    hook text mentions another memory's file must still resolve to its own slug,
    or the importer would hand `beta`'s title to `alpha`."""
    src = tmp_path / "memory"
    _write_source(src, "alpha", "First fact.", "Alpha body.")
    _write_source(src, "beta", "Second fact.", "Beta body.")
    (src / "MEMORY.md").write_text(
        "- [Alpha Fact](alpha.md) — see also [Beta](beta.md)\n"
        "- [Beta Fact](beta.md) — second\n",
        encoding="utf-8",
    )
    scan = claude_import.read_source(src)
    assert [(m.slug, m.title) for m in scan.memories] == [
        ("alpha", "Alpha Fact"),
        ("beta", "Beta Fact"),
    ]


def test_read_source_falls_back_to_the_slug_for_orphan_files(tmp_path: Path):
    """A memory file with no index entry still imports; its slug becomes the
    title, which is what a hand-written memory would have looked like anyway."""
    src = tmp_path / "memory"
    _write_source(src, "orphan", "No index line.", "Orphan body.")
    scan = claude_import.read_source(src)
    assert [(m.slug, m.title) for m in scan.memories] == [("orphan", "orphan")]


def test_read_source_skips_the_index_itself_and_reports_unparseable(tmp_path: Path):
    src = tmp_path / "memory"
    _write_source(src, "good", "Fine.", "Good body.")
    (src / "junk.md").write_text("no frontmatter here\n", encoding="utf-8")
    (src / "MEMORY.md").write_text("- [Good](good.md) — hook\n", encoding="utf-8")
    scan = claude_import.read_source(src)
    assert [m.slug for m in scan.memories] == ["good"]
    assert any("junk.md" in p for p in scan.problems)


def test_read_source_reports_undecodable_file(tmp_path: Path):
    src = tmp_path / "memory"
    _write_source(src, "good", "Fine.", "Good body.")
    (src / "binary.md").write_bytes(b"\xff\xfe\x00bad")
    scan = claude_import.read_source(src)
    assert [m.slug for m in scan.memories] == ["good"]
    assert any("binary.md" in p for p in scan.problems)


def test_read_source_on_empty_dir_is_empty(tmp_path: Path):
    src = tmp_path / "memory"
    src.mkdir()
    scan = claude_import.read_source(src)
    assert scan.memories == () and scan.problems == ()


def _mem(slug, title):
    return claude_import.ImportedMemory(
        slug=slug, title=title, description="d", mem_type="project", body="b"
    )


def _state(slugs=(), entries=()):
    return claude_import.TargetState(slugs=frozenset(slugs), entries=tuple(entries))


def test_plan_import_marks_fresh_memories_as_import():
    plan = claude_import.plan_import([_mem("alpha", "Alpha")], state=_state(), force=False)
    assert [(p.action, p.slug) for p in plan] == [("import", "alpha")]
    assert plan[0].reason == ""
    assert plan[0].target_slug == "alpha"


def test_plan_import_skips_an_existing_slug():
    plan = claude_import.plan_import(
        [_mem("alpha", "Alpha")], state=_state(slugs={"alpha"}), force=False
    )
    assert plan[0].action == "skip"
    assert "already present" in plan[0].reason


def test_plan_import_skips_a_title_claimed_by_a_different_slug():
    """The clobber allocate_slug would otherwise cause: the source slug is
    free, but the target already stores that title under another slug, so
    save_memory would write into *that* file."""
    plan = claude_import.plan_import(
        [_mem("alpha", "Shared Title")],
        state=_state(slugs={"other"}, entries=[("Shared Title", "other")]),
        force=False,
    )
    assert plan[0].action == "skip"
    assert "other" in plan[0].reason
    assert plan[0].target_slug == "other"


def test_plan_import_skips_a_title_that_only_matches_after_normalization():
    """The C1 regression guard at the pure-planner level: `index_title` strips
    `[]()` and collapses whitespace runs, so these two titles are the SAME index
    entry as far as `allocate_slug` is concerned. A raw-equality guard let this
    through as a clean import and then wrote into `other.md`."""
    for title in ("Shared (Title)", "Shared  Title", "[Shared] Title"):
        plan = claude_import.plan_import(
            [_mem("alpha", title)],
            state=_state(slugs={"other"}, entries=[("Shared Title", "other")]),
            force=False,
        )
        assert plan[0].action == "skip", title
        assert plan[0].target_slug == "other", title


def test_plan_import_skips_a_source_filename_that_only_collides_once_slugified():
    """The slug axis of the same defect: the target file is `my-note.md` and the
    index is stale (empty), so only the file read sees it. `save_memory`
    slugifies `My_Note` to `my-note` and would land on it."""
    plan = claude_import.plan_import(
        [_mem("My_Note", "Totally Different Title")],
        state=_state(slugs={"my-note"}),
        force=False,
    )
    assert plan[0].action == "skip"
    assert "already present" in plan[0].reason
    assert plan[0].target_slug == "my-note"


def test_plan_import_allows_a_title_already_owned_by_the_same_slug():
    """Re-importing the same memory is a refresh, not a cross-slug clobber, so
    it is an ordinary overwrite decision rather than a title conflict."""
    plan = claude_import.plan_import(
        [_mem("alpha", "Alpha")], state=_state(entries=[("Alpha", "alpha")]), force=False
    )
    assert plan[0].action == "import"


def test_plan_import_force_turns_conflicts_into_overwrites():
    plan = claude_import.plan_import(
        [_mem("alpha", "Alpha"), _mem("beta", "Beta")],
        state=_state(slugs={"alpha", "gamma"}, entries=[("Beta", "gamma")]),
        force=True,
    )
    assert [(p.action, p.slug) for p in plan] == [("overwrite", "alpha"), ("overwrite", "beta")]
    assert [p.target_slug for p in plan] == ["alpha", "gamma"]


def test_plan_import_preserves_source_order():
    plan = claude_import.plan_import(
        [_mem("c", "C"), _mem("a", "A")], state=_state(), force=False
    )
    assert [p.slug for p in plan] == ["c", "a"]


def test_plan_import_sees_a_collision_between_two_sources(tmp_path: Path):
    """I1: nothing exists in the target at all, but the two sources collide with
    each other — `Auth (v2)` normalizes onto `Auth v2`. The plan is decided
    against a snapshot, so unless accepted entries are folded forward the second
    source is planned as a clean import and then overwrites the first."""
    sources = [_mem("auth-old", "Auth (v2)"), _mem("auth-v2", "Auth v2")]
    plan = claude_import.plan_import(sources, state=_state(), force=False)
    assert [(p.action, p.slug) for p in plan] == [("import", "auth-old"), ("skip", "auth-v2")]
    assert "auth-old" in plan[1].reason


def test_plan_import_folds_a_same_slug_refresh_forward_without_a_false_conflict():
    """Threading state forward must not invent conflicts: two sources that do
    not collide both plan as imports even though the first one mutated the
    running state."""
    plan = claude_import.plan_import(
        [_mem("alpha", "Alpha"), _mem("beta", "Beta")], state=_state(), force=False
    )
    assert [p.action for p in plan] == ["import", "import"]


def test_target_state_reads_slugs_and_index_entries(tmp_path: Path):
    from marim_harness.workspace import memory

    scope = memory.project_scope(tmp_path)
    memory.save_memory(
        scope, name="Alpha", description="d", mem_type="project", body="b", title="Alpha"
    )
    state = claude_import.target_state(scope)
    assert state.slugs == frozenset({"alpha"})
    assert state.entries == (("Alpha", "alpha"),)


def test_target_state_keeps_duplicate_titles_in_file_order(tmp_path: Path):
    """`allocate_slug` resolves a duplicated title to the FIRST entry; a
    `title -> slug` dict would have kept the last, disagreeing with the writer."""
    from marim_harness.workspace import memory

    scope = memory.project_scope(tmp_path)
    scope.root.mkdir(parents=True)
    (scope.root / "MEMORY.md").write_text(
        "- [Dup](first.md) — a\n- [Dup](second.md) — b\n", encoding="utf-8"
    )
    state = claude_import.target_state(scope)
    assert state.entries == (("Dup", "first"), ("Dup", "second"))


def test_target_state_on_missing_dir_is_empty(tmp_path: Path):
    from marim_harness.workspace import memory

    state = claude_import.target_state(memory.project_scope(tmp_path))
    assert state.slugs == frozenset() and state.entries == ()


def test_with_saved_refreshes_an_existing_entry_in_place():
    state = _state(slugs={"alpha"}, entries=[("Old", "alpha"), ("Beta", "beta")])
    got = state.with_saved(slug="alpha", title="New  (Title)")
    assert got.entries == (("New Title", "alpha"), ("Beta", "beta"))
    assert got.slugs == frozenset({"alpha"})


def test_with_saved_appends_a_new_entry_with_the_index_form_of_the_title():
    got = _state().with_saved(slug="alpha", title="A  [weird] (title)")
    assert got.entries == (("A weird title", "alpha"),)
    assert got.slugs == frozenset({"alpha"})


def test_apply_plan_writes_files_and_index(tmp_path: Path):
    from marim_harness.workspace import memory

    scope = memory.project_scope(tmp_path)
    sources = [_mem("alpha", "Alpha Fact"), _mem("beta", "Beta Fact")]
    plan = claude_import.plan_import(sources, state=claude_import.target_state(scope), force=False)
    result = claude_import.apply_plan(plan, sources, scope)

    assert result.imported == ("alpha", "beta")
    assert result.skipped == () and result.failed == ()
    written = (scope.root / "alpha.md").read_text(encoding="utf-8")
    assert "name: alpha" in written
    assert "type: project" in written
    index = (scope.root / "MEMORY.md").read_text(encoding="utf-8")
    assert "- [Alpha Fact](alpha.md)" in index
    assert "- [Beta Fact](beta.md)" in index


def test_apply_plan_does_not_write_skipped_memories(tmp_path: Path):
    from marim_harness.workspace import memory

    scope = memory.project_scope(tmp_path)
    memory.save_memory(
        scope, name="alpha", description="mine", mem_type="project", body="marim body",
        title="Alpha Fact",
    )
    sources = [_mem("alpha", "Alpha Fact")]
    plan = claude_import.plan_import(sources, state=claude_import.target_state(scope), force=False)
    result = claude_import.apply_plan(plan, sources, scope)

    assert result.imported == () and result.skipped == ("alpha",)
    assert "marim body" in (scope.root / "alpha.md").read_text(encoding="utf-8")


def test_apply_plan_overwrites_under_force(tmp_path: Path):
    from marim_harness.workspace import memory

    scope = memory.project_scope(tmp_path)
    memory.save_memory(
        scope, name="alpha", description="mine", mem_type="project", body="marim body",
        title="Alpha Fact",
    )
    sources = [_mem("alpha", "Alpha Fact")]
    plan = claude_import.plan_import(sources, state=claude_import.target_state(scope), force=True)
    result = claude_import.apply_plan(plan, sources, scope)

    assert result.imported == ("alpha",)
    assert (scope.root / "alpha.md").read_text(encoding="utf-8").rstrip().endswith("b")


def test_apply_plan_records_a_failed_write(tmp_path: Path, monkeypatch):
    """save_memory returns None instead of raising when the write fails; the
    importer must surface that as a failure rather than count it as imported."""
    from marim_harness.workspace import memory

    monkeypatch.setattr(claude_import, "save_memory", lambda *a, **k: None)
    scope = memory.project_scope(tmp_path)
    sources = [_mem("alpha", "Alpha")]
    plan = claude_import.plan_import(sources, state=claude_import.target_state(scope), force=False)
    result = claude_import.apply_plan(plan, sources, scope)

    assert result.failed == ("alpha",) and result.imported == ()


# --- plan/writer agreement, end to end ------------------------------------
#
# Every test above this line feeds the pure planner hand-built state. That is
# exactly how a guard that compared raw slugs and raw titles against a writer
# that normalizes them shipped green: the planner's branch logic was right, but
# nothing proved `target_state`'s output was in the form `allocate_slug`
# compares. These run the real chain — save_memory-built target, real source
# store, target_state -> plan_import -> apply_plan — and assert on the bytes on
# disk afterward.


def _marim_memory(tmp_path: Path, *, slug: str, title: str, body: str):
    from marim_harness.workspace import memory

    scope = memory.project_scope(tmp_path)
    memory.save_memory(
        scope, name=slug, description="marim's own note", mem_type="project",
        body=body, title=title,
    )
    return scope


def _import(scope, sources, *, force=False):
    plan = claude_import.plan_import(
        sources, state=claude_import.target_state(scope), force=force
    )
    return plan, claude_import.apply_plan(plan, sources, scope)


def _source_store(tmp_path: Path, entries):
    """A real Claude memory dir (files + index), read back through read_source."""
    src = tmp_path / "claude-memory"
    for slug, _title, body in entries:
        _write_source(src, slug, "claude desc", body)
    (src / "MEMORY.md").write_text(
        "".join(f"- [{title}]({slug}.md) — claude hook\n" for slug, title, _ in entries),
        encoding="utf-8",
    )
    return claude_import.read_source(src).memories


def test_import_does_not_clobber_a_marim_memory_whose_title_only_matches_normalized(
    tmp_path: Path,
):
    """C1, title axis. `Deploy (notes)` and `Deploy  notes` both normalize onto
    the marim memory titled `Deploy notes`, so save_memory would write into
    `marim-deploy.md`. Reproduced destroying the user's own memory on the
    default, non-force path before the guard ran the real allocator."""
    for source_title in ("Deploy (notes)", "Deploy  notes"):
        ws = tmp_path / source_title.replace(" ", "_").replace("(", "").replace(")", "")
        ws.mkdir()
        scope = _marim_memory(ws, slug="marim-deploy", title="Deploy notes", body="MARIM ORIGINAL")
        sources = _source_store(ws, [("alpha", source_title, "CLAUDE BODY")])

        plan, result = _import(scope, sources)

        assert [p.action for p in plan] == ["skip"], source_title
        assert "marim-deploy" in plan[0].reason, source_title
        assert result.imported == () and result.skipped == ("alpha",)
        kept = (scope.root / "marim-deploy.md").read_text(encoding="utf-8")
        assert "MARIM ORIGINAL" in kept and "CLAUDE BODY" not in kept
        assert not (scope.root / "alpha.md").exists()


def test_import_does_not_clobber_a_marim_file_a_source_filename_slugifies_onto(
    tmp_path: Path,
):
    """C1, slug axis. The target index is stale (emptied by hand), so only the
    file listing knows `my-note.md` is there; the source filename `My_Note`
    slugifies straight onto it."""
    scope = _marim_memory(tmp_path, slug="my-note", title="My Note", body="MARIM ORIGINAL")
    (scope.root / "MEMORY.md").write_text("# Memory Index\n", encoding="utf-8")
    sources = _source_store(tmp_path, [("My_Note", "Totally Different Title", "CLAUDE BODY")])

    plan, result = _import(scope, sources)

    assert [p.action for p in plan] == ["skip"]
    assert "already present" in plan[0].reason
    assert result.skipped == ("My_Note",)
    kept = (scope.root / "my-note.md").read_text(encoding="utf-8")
    assert "MARIM ORIGINAL" in kept and "CLAUDE BODY" not in kept


def test_import_does_not_let_two_sources_overwrite_each_other(tmp_path: Path):
    """I1, end to end: an empty target, two sources whose titles normalize
    together. Both used to plan as clean imports and one silently won."""
    from marim_harness.workspace import memory

    scope = memory.project_scope(tmp_path)
    sources = _source_store(tmp_path, [("auth-old", "Auth (v2)", "BODY B"),
                                       ("auth-v2", "Auth v2", "BODY A")])

    plan, result = _import(scope, sources)

    assert [(p.action, p.slug) for p in plan] == [("import", "auth-old"), ("skip", "auth-v2")]
    assert result.imported == ("auth-old",) and result.skipped == ("auth-v2",)
    written = sorted(p.name for p in scope.root.glob("*.md"))
    assert written == ["MEMORY.md", "auth-old.md"]
    assert "BODY B" in (scope.root / "auth-old.md").read_text(encoding="utf-8")


def test_force_reports_the_file_it_actually_wrote(tmp_path: Path):
    """M2: under a title conflict save_memory writes the *incumbent's* file, so
    reporting the source slug would name a file the run never created."""
    scope = _marim_memory(tmp_path, slug="marim-deploy", title="Deploy notes", body="MARIM")
    sources = _source_store(tmp_path, [("alpha", "Deploy notes", "CLAUDE BODY")])

    plan, result = _import(scope, sources, force=True)

    assert plan[0].action == "overwrite" and plan[0].target_slug == "marim-deploy"
    assert result.imported == ("marim-deploy",)
    assert not (scope.root / "alpha.md").exists()
    assert "CLAUDE BODY" in (scope.root / "marim-deploy.md").read_text(encoding="utf-8")
