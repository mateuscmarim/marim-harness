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
