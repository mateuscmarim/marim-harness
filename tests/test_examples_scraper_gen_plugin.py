"""Regression guard for the bundled ``examples/scraper-gen`` plugin: keep its
manifest, agents, and skill parseable by marim's own loaders so the example
can't silently rot as the plugin format evolves."""

from pathlib import Path

from marim_harness.plugins.manifest import load_manifest

PLUGIN_ROOT = Path(__file__).resolve().parents[1] / "examples" / "scraper-gen"


def test_manifest_loads():
    m = load_manifest(PLUGIN_ROOT)
    assert m.name == "scraper-gen"
    assert m.version == "0.1.0"
