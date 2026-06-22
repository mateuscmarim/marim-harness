"""Install, update, and manage the lifecycle of plugins.

Installing copies (or, with ``link``, symlinks) a plugin's directory into the
target scope's ``plugins/`` cache, validates its manifest strictly, and records
it in the scope registry. A plugin with no executable parts (hooks/MCP) is
auto-trusted; one with executable parts is trusted only when ``trust`` is set.
Git sources are shallow-cloned to a temp dir and copied in, recording the
resolved commit SHA."""

import logging
import shutil
import subprocess
import tempfile
from pathlib import Path

from .discovery import has_executable, plugin_bundle_summary
from .manifest import ManifestError, PluginManifest, load_manifest
from .state import (
    InstalledPlugin,
    global_plugins_dir,
    load_state,
    project_plugins_dir,
    save_state,
)

logger = logging.getLogger(__name__)


class InstallError(Exception):
    """An install/update/lifecycle operation failed."""


def scope_dir(scope: str, workspace_root) -> Path:
    if scope == "global":
        return global_plugins_dir()
    if scope == "project":
        return project_plugins_dir(workspace_root)
    raise InstallError(f"unknown scope: {scope!r} (use 'global' or 'project')")


def is_git_source(source: str) -> bool:
    s = source.strip()
    if s.startswith(("http://", "https://", "git://", "ssh://")):
        return True
    if s.startswith("git@"):
        return True
    return s.endswith(".git")


def _run_git(args: list[str], cwd: Path | None = None) -> str:
    try:
        out = subprocess.run(
            ["git", *args],
            cwd=str(cwd) if cwd else None,
            check=True,
            capture_output=True,
            text=True,
        )
    except FileNotFoundError as exc:
        raise InstallError("git is not installed") from exc
    except subprocess.CalledProcessError as exc:
        raise InstallError(f"git {' '.join(args)} failed: {exc.stderr.strip()}") from exc
    return out.stdout.strip()


def _clone_git(source: str, dest: Path, ref: str | None) -> dict:
    """Clone ``source`` into ``dest`` and return a source record with the resolved SHA."""
    args = ["clone", "--depth", "1"]
    if ref:
        args += ["--branch", ref]
    args += [source, str(dest)]
    _run_git(args)
    sha = _run_git(["rev-parse", "HEAD"], cwd=dest)
    record: dict = {"type": "git", "url": source, "sha": sha}
    if ref:
        record["ref"] = ref
    return record


def _validated_manifest(plugin_dir: Path) -> PluginManifest:
    try:
        return load_manifest(plugin_dir)
    except ManifestError as exc:
        raise InstallError(str(exc)) from exc


def _materialize(src_dir: Path, dest: Path, *, link: bool) -> None:
    if dest.exists() or dest.is_symlink():
        if dest.is_symlink() or dest.is_file():
            dest.unlink()
        else:
            shutil.rmtree(dest)
    dest.parent.mkdir(parents=True, exist_ok=True)
    if link:
        dest.symlink_to(src_dir.resolve(), target_is_directory=True)
    else:
        shutil.copytree(src_dir, dest)


def install_plugin(
    source: str,
    *,
    scope: str,
    workspace_root,
    trust: bool,
    link: bool = False,
    name_override: str | None = None,
    now: str,
    _force_git: bool = False,
) -> InstalledPlugin:
    """Install ``source`` (a local dir or git URL) into ``scope``. Returns the
    written registry record."""
    target_root = scope_dir(scope, workspace_root)
    use_git = _force_git or is_git_source(source)

    with tempfile.TemporaryDirectory() as tmp:
        if use_git:
            if link:
                raise InstallError("--link is only valid for local sources")
            staging = Path(tmp) / "clone"
            source_record = _clone_git(source, staging, ref=None)
        else:
            staging = Path(source)
            if not staging.is_dir():
                raise InstallError(f"not a directory: {source}")
            source_record = {"type": "local", "path": str(staging.resolve())}

        manifest = _validated_manifest(staging)
        name = name_override or manifest.name
        summary = plugin_bundle_summary(manifest)
        trusted = True if not has_executable(summary) else bool(trust)

        dest = target_root / name
        # For git, always copy (link only applies to local sources).
        _materialize(staging, dest, link=link and not use_git)

    record = InstalledPlugin(
        name=name,
        version=manifest.version,
        source=source_record,
        enabled=True,
        trusted=trusted,
        linked=bool(link and not use_git),
        installed_at=now,
    )
    state = load_state(target_root)
    state[name] = record
    save_state(target_root, state)
    return record


def _mutate(name: str, scope: str, workspace_root, fn) -> bool:
    target_root = scope_dir(scope, workspace_root)
    state = load_state(target_root)
    rec = state.get(name)
    if rec is None:
        return False
    fn(rec)
    save_state(target_root, state)
    return True


def set_enabled(name: str, *, scope: str, workspace_root, enabled: bool) -> bool:
    return _mutate(name, scope, workspace_root, lambda r: setattr(r, "enabled", enabled))


def set_trusted(name: str, *, scope: str, workspace_root, trusted: bool) -> bool:
    return _mutate(name, scope, workspace_root, lambda r: setattr(r, "trusted", trusted))


def remove_plugin(name: str, *, scope: str, workspace_root) -> bool:
    target_root = scope_dir(scope, workspace_root)
    state = load_state(target_root)
    if name not in state:
        return False
    dest = target_root / name
    if dest.is_symlink() or dest.is_file():
        dest.unlink()
    elif dest.exists():
        shutil.rmtree(dest)
    del state[name]
    save_state(target_root, state)
    return True


def update_plugin(name: str, *, scope: str, workspace_root, now: str) -> InstalledPlugin:
    """Re-fetch a git-sourced plugin to the latest of its ref. Local/linked
    plugins cannot be updated this way."""
    target_root = scope_dir(scope, workspace_root)
    state = load_state(target_root)
    rec = state.get(name)
    if rec is None:
        raise InstallError(f"plugin not installed: {name}")
    if rec.source.get("type") != "git":
        raise InstallError(f"{name} was not installed from git; reinstall to update")
    url = rec.source["url"]
    ref = rec.source.get("ref")
    with tempfile.TemporaryDirectory() as tmp:
        staging = Path(tmp) / "clone"
        source_record = _clone_git(url, staging, ref=ref)
        manifest = _validated_manifest(staging)
        _materialize(staging, target_root / name, link=False)
    rec.version = manifest.version
    rec.source = source_record
    rec.installed_at = now
    save_state(target_root, state)
    return rec
