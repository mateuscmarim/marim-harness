import json
from pathlib import Path

from marim_harness.lsp.provider import LspRegistry
from marim_harness.plugins.discovery import plugin_lsp_providers
from marim_harness.plugins.state import (
    InstalledPlugin,
    global_plugins_dir,
    project_plugins_dir,
    save_state,
)


def _install_project_plugin(ws: Path, name: str, lsp: dict, *, trusted: bool):
    pdir = project_plugins_dir(ws) / name
    (pdir / ".marim-plugin").mkdir(parents=True)
    (pdir / ".marim-plugin" / "plugin.json").write_text(json.dumps({"name": name, "lsp": lsp}))
    save_state(
        project_plugins_dir(ws),
        {name: InstalledPlugin(name=name, version=None, source={}, enabled=True, trusted=trusted)},
    )


def test_project_lsp_provider_overrides_global_on_language_collision(tmp_path, monkeypatch):
    """LspRegistry takes later providers, so plugins must be global-first."""
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "cfg"))
    global_dir = global_plugins_dir()
    global_plugin = global_dir / "global-go"
    (global_plugin / ".marim-plugin").mkdir(parents=True)
    (global_plugin / ".marim-plugin" / "plugin.json").write_text(
        json.dumps(
            {
                "name": "global-go",
                "lsp": {"language": "go", "extensions": [".go"], "command": "global-gopls"},
            }
        )
    )
    save_state(
        global_dir,
        {"global-go": InstalledPlugin("global-go", None, {}, enabled=True, trusted=True)},
    )
    _install_project_plugin(
        tmp_path,
        "project-go",
        {"language": "go", "extensions": [".go"], "command": "project-gopls"},
        trusted=True,
    )

    providers = plugin_lsp_providers(tmp_path, trust_project=True)
    assert [provider.source for provider in providers] == ["global", "project"]
    assert LspRegistry(providers).provider_for("go").command == "project-gopls"


def test_project_plugin_lsp_gated_by_trust(tmp_path):
    _install_project_plugin(
        tmp_path,
        "go-lsp",
        {"language": "go", "extensions": [".go"], "command": "gopls"},
        trusted=True,
    )
    # Per-plugin trusted bit set, but project gate off ⇒ withheld.
    assert plugin_lsp_providers(tmp_path, trust_project=False) == []
    # Project gate on ⇒ contributed.
    provs = plugin_lsp_providers(tmp_path, trust_project=True)
    assert [p.language for p in provs] == ["go"]
    assert provs[0].source == "project"


def test_untrusted_project_plugin_withheld_even_with_gate(tmp_path):
    _install_project_plugin(
        tmp_path,
        "go-lsp",
        {"language": "go", "extensions": [".go"], "command": "gopls"},
        trusted=False,
    )
    assert plugin_lsp_providers(tmp_path, trust_project=True) == []


def test_third_party_backend_key_ignored(tmp_path):
    # A malicious/mistaken third-party plugin using the bundled-only backend key
    # contributes nothing (lenient parse drops it), not a basedpyright hijack.
    _install_project_plugin(
        tmp_path,
        "evil",
        {"language": "python", "extensions": [".py"], "backend": "basedpyright"},
        trusted=True,
    )
    assert plugin_lsp_providers(tmp_path, trust_project=True) == []


def test_plugin_root_substitution(tmp_path):
    _install_project_plugin(
        tmp_path,
        "wrapped",
        {
            "language": "go",
            "extensions": [".go"],
            "command": "${MARIM_PLUGIN_ROOT}/bin/gopls-wrapper",
        },
        trusted=True,
    )
    (prov,) = plugin_lsp_providers(tmp_path, trust_project=True)
    assert prov.command.endswith("/bin/gopls-wrapper")
    assert "${MARIM_PLUGIN_ROOT}" not in prov.command
