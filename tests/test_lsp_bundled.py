from marim_harness.lsp.bundled import bundled_lsp_providers


def test_bundled_covers_expected_extensions():
    provs = bundled_lsp_providers()
    ext_to_lang = {e: p.language for p in provs for e in p.extensions}
    # Every extension the old _EXT_TO_LANG covered must still resolve.
    for ext in [".py", ".ts", ".tsx", ".js", ".jsx", ".mjs", ".cjs",
                ".java", ".cpp", ".cc", ".cxx", ".c", ".h", ".hpp", ".hh"]:
        assert ext in ext_to_lang, ext


def test_bundled_python_uses_basedpyright_and_python_checks():
    py = next(p for p in bundled_lsp_providers() if p.language == "python")
    assert py.backend == "basedpyright"
    assert py.diagnostics == "python-checks"
    assert py.probe == ("basedpyright-langserver", "jedi-language-server")
    assert py.source == "bundled"


def test_bundled_java_is_auto_provided():
    java = next(p for p in bundled_lsp_providers() if p.language == "java")
    assert java.probe == ()
    assert java.backend == "multilspy:java"


def test_all_bundled_are_bundled_source():
    assert all(p.source == "bundled" for p in bundled_lsp_providers())
