from marim_harness.lsp import registry


def test_language_for_known_extensions():
    assert registry.language_for("src/mod.py") == "python"
    assert registry.language_for("a/b/Comp.tsx") == "typescript"
    assert registry.language_for("x.ts") == "typescript"
    assert registry.language_for("x.jsx") == "javascript"
    assert registry.language_for("Main.java") == "java"
    assert registry.language_for("engine.cpp") == "cpp"
    assert registry.language_for("util.hpp") == "cpp"


def test_language_for_unknown_or_extensionless():
    assert registry.language_for("README.md") is None
    assert registry.language_for("Makefile") is None
    assert registry.language_for("noext") is None


def test_language_for_is_case_insensitive():
    assert registry.language_for("FOO.PY") == "python"


def test_language_for_ignores_dotted_directory():
    # A dot in a parent directory must not be mistaken for the file's extension:
    # only the basename's suffix counts.
    assert registry.language_for("src.v2/Makefile") is None
    assert registry.language_for("foo.bar/baz") is None
    assert registry.language_for("pkg.v2/mod.py") == "python"
    assert registry.language_for("a.b.c/Comp.tsx") == "typescript"


def test_availability_unsupported_language():
    a = registry.availability("cobol")
    assert a.available is False
    assert a.hint


def test_availability_auto_provided_language_is_available():
    # java is auto-downloaded by multilspy; no PATH probe required.
    assert registry.availability("java").available is True


def test_availability_path_probed(monkeypatch):
    monkeypatch.setattr(
        registry.shutil, "which", lambda b: "/usr/bin/clangd" if b == "clangd" else None
    )
    assert registry.availability("cpp").available is True
    monkeypatch.setattr(registry.shutil, "which", lambda b: None)
    cpp = registry.availability("cpp")
    assert cpp.available is False
    assert "clangd" in cpp.hint


def test_locally_installed_excludes_auto_download(monkeypatch):
    monkeypatch.setattr(registry.shutil, "which", lambda b: "/x" if b == "clangd" else None)
    langs = registry.locally_installed_languages()
    assert "cpp" in langs
    assert "java" not in langs  # java has no PATH probe (auto-download only)


def test_workspace_languages_scans_by_extension(tmp_path):
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "a.py").write_text("x = 1")
    (tmp_path / "web").mkdir()
    (tmp_path / "web" / "App.tsx").write_text("")
    (tmp_path / "README.md").write_text("")
    (tmp_path / "Makefile").write_text("")
    assert registry.workspace_languages(tmp_path) == {"python", "typescript"}


def test_workspace_languages_prunes_hidden_and_dependency_dirs(tmp_path):
    # Dependency/cache trees say nothing about what the user edits: a pure-docs
    # repo with a .venv full of .py files must not report python.
    for d in (".venv", ".git", "node_modules", "__pycache__", "venv"):
        sub = tmp_path / d / "inner"
        sub.mkdir(parents=True)
        (sub / "mod.py").write_text("")
    (tmp_path / "notes.md").write_text("")
    assert registry.workspace_languages(tmp_path) == set()


def test_workspace_languages_empty_or_missing_root(tmp_path):
    assert registry.workspace_languages(tmp_path) == set()
    assert registry.workspace_languages(tmp_path / "nope") == set()


def test_workspace_languages_drops_incidental_minority(tmp_path):
    # A handful of vendored/example files in another language must not count
    # as workspace coverage: 2 ts files against 30 py is noise, and reporting
    # typescript here would let a global typescript-language-server satisfy
    # the build-time gate for what is really a python repo.
    for i in range(30):
        (tmp_path / f"m{i:02}.py").write_text("")
    (tmp_path / "a.ts").write_text("")
    (tmp_path / "b.ts").write_text("")
    assert registry.workspace_languages(tmp_path) == {"python"}


def test_workspace_languages_keeps_minority_share(tmp_path):
    # A small repo where the minority language still holds a real share
    # (2/12 ≈ 17%) keeps both languages.
    for i in range(10):
        (tmp_path / f"m{i:02}.py").write_text("")
    (tmp_path / "a.ts").write_text("")
    (tmp_path / "b.ts").write_text("")
    assert registry.workspace_languages(tmp_path) == {"python", "typescript"}


def test_workspace_languages_keeps_minority_by_absolute_count(tmp_path):
    # In a huge repo even a sub-10% share can be a real sub-project; an
    # absolute floor (20 files) keeps it.
    for i in range(300):
        (tmp_path / f"m{i:03}.py").write_text("")
    for i in range(20):
        (tmp_path / f"w{i:02}.ts").write_text("")
    assert registry.workspace_languages(tmp_path) == {"python", "typescript"}


def test_workspace_languages_entry_cap_bounds_the_scan(tmp_path):
    # Files are visited in sorted order, so a cap smaller than the junk
    # prefix stops the walk before ever reaching zz.py.
    for i in range(10):
        (tmp_path / f"f{i:02}.txt").write_text("")
    (tmp_path / "zz.py").write_text("")
    assert registry.workspace_languages(tmp_path, max_entries=5) == set()
    assert registry.workspace_languages(tmp_path) == {"python"}
