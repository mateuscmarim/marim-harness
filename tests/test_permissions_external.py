"""decide_external: the transport-neutral approval table shared by the
codex-cli and claude-cli brokers (spec §Shared policy core)."""

from __future__ import annotations

from pathlib import Path

from marim_harness.runtime.permissions import (
    PLAN_READ_ONLY,
    Decision,
    ExternalRequest,
    Mode,
    decide_external,
    within_root,
)


def _req(mutating: bool, *paths: Path) -> ExternalRequest:
    return ExternalRequest(mutating=mutating, paths=tuple(paths))


def test_plan_denies_outbound_network_even_though_it_is_not_mutating(tmp_path: Path):
    """Plan mode is read-only *local* research: egress is denied there for the
    same reason ``_plan_decision`` denies marim's own net tools."""
    net = ExternalRequest(mutating=False, network=True)
    d = decide_external(Mode.plan, net, tmp_path, None)
    assert d == Decision(accept=False, reason=PLAN_READ_ONLY)
    assert not d.ask
    for mode in (Mode.ask, Mode.auto):
        assert decide_external(mode, net, tmp_path, None) == Decision(accept=True)


def test_plan_accepts_reads_and_denies_every_mutation(tmp_path: Path):
    inside = tmp_path / "a.txt"
    assert decide_external(Mode.plan, _req(False), tmp_path, None) == Decision(accept=True)
    for req in (_req(True, inside), _req(True, tmp_path.parent / "x"), _req(True)):
        d = decide_external(Mode.plan, req, tmp_path, None)
        assert d == Decision(accept=False, reason=PLAN_READ_ONLY)
        assert not d.ask


def test_auto_accepts_inside_and_unknown_paths_but_asks_outside(tmp_path: Path):
    root = tmp_path / "ws"
    root.mkdir()
    pad = tmp_path / "pad"
    pad.mkdir()
    assert decide_external(Mode.auto, _req(True, root / "f"), root, pad) == Decision(accept=True)
    assert decide_external(Mode.auto, _req(True, pad / "f"), root, pad) == Decision(accept=True)
    assert decide_external(Mode.auto, _req(True), root, pad) == Decision(accept=True)
    stray = tmp_path / "elsewhere" / "f"
    d = decide_external(Mode.auto, _req(True, root / "ok", stray), root, pad)
    assert d.ask and not d.accept
    assert d.reason == f"outside workspace: {stray}"


def test_ask_accepts_reads_and_scratchpad_only_writes_else_asks(tmp_path: Path):
    root = tmp_path / "ws"
    root.mkdir()
    pad = tmp_path / "pad"
    pad.mkdir()
    assert decide_external(Mode.ask, _req(False), root, pad) == Decision(accept=True)
    d = decide_external(Mode.ask, _req(True, pad / "notes.md"), root, pad)
    assert d == Decision(accept=True, reason="scratchpad write")
    assert decide_external(Mode.ask, _req(True, root / "f"), root, pad).ask
    assert decide_external(Mode.ask, _req(True, pad / "f", root / "g"), root, pad).ask
    assert decide_external(Mode.ask, _req(True), root, pad).ask
    # No scratchpad bound: a mutating request always prompts.
    assert decide_external(Mode.ask, _req(True, pad / "f"), root, None).ask


def test_within_root_rejects_dotdot_and_symlink_escape(tmp_path: Path):
    root = tmp_path / "ws"
    root.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    (root / "link").symlink_to(outside)
    assert within_root(root / "sub" / "f.txt", root)
    assert not within_root(root / ".." / "outside" / "f", root)
    assert not within_root(root / "link" / "f", root)
    assert not within_root(root / "f", None)
