"""Tests for build_lsp_registry: the bundled+plugin registry assembled at
bootstrap time and threaded through the builder/gate/LspManager (Task 7)."""

from marim_harness.runtime.bootstrap import build_lsp_registry


def test_registry_includes_bundled(tmp_path):
    reg = build_lsp_registry(tmp_path, trust_project=False)
    assert reg.provider_for("python") is not None
    assert reg.provider_for("java") is not None


def test_gate_uses_registry(tmp_path):
    (tmp_path / "a.py").write_text("x = 1\n")
    (tmp_path / "b.py").write_text("y = 2\n")
    reg = build_lsp_registry(tmp_path, trust_project=False)
    # python present in workspace; its provider exists (availability depends on
    # PATH, but workspace_languages must detect it).
    assert "python" in reg.workspace_languages(tmp_path)


def test_registry_respects_trust_project(tmp_path, monkeypatch):
    """trust_project is threaded through to plugin_lsp_providers unmodified —
    a project-scope plugin's LSP contribution is withheld when the project is
    untrusted, same as its MCP/hooks contributions."""
    calls: list[bool] = []

    def _fake_plugin_providers(workspace_root, *, trust_project=False):
        calls.append(trust_project)
        return []

    monkeypatch.setattr(
        "marim_harness.runtime.bootstrap.plugin_lsp_providers", _fake_plugin_providers
    )

    build_lsp_registry(tmp_path, trust_project=True)
    build_lsp_registry(tmp_path, trust_project=False)

    assert calls == [True, False]
