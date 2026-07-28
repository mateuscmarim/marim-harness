"""``marim serve`` — run marim as a long-lived HTTP daemon (REST + WebSocket).

Binds 127.0.0.1 by default; expose it via a reverse proxy or tailscale and
authenticate with the bearer token printed at startup (persisted 0600 under
the server state dir). Requires the ``serve`` extra (starlette + uvicorn)."""

import argparse
import os
import sys
from dataclasses import dataclass
from pathlib import Path

from ..branding import banner_enabled, color_enabled, field_block, package_version, wordmark_block


def _default_state_dir() -> Path:
    base = os.environ.get("XDG_DATA_HOME")
    root = Path(base) if base else Path.home() / ".local" / "share"
    return root / "marim-harness" / "server"


def _display_path(path: Path) -> str:
    """``path`` with ``$HOME`` collapsed to ``~`` — state-dir paths are long and
    the interesting part is the tail."""
    try:
        return f"~/{path.relative_to(Path.home())}"
    except ValueError:
        return str(path)


@dataclass(frozen=True)
class ServeStartup:
    """What a launching operator needs to see: where it bound, where the bearer
    token landed, which workspaces root it adopted, and the eviction TTL. The
    last two come from flags that were previously invisible at startup, so you
    couldn't tell from the terminal which root a running daemon had picked up.

    Deliberately never prints the token *value* — only its path. Startup output
    lands in scrollback, screenshots, and logs; the secret shouldn't.
    """

    url: str
    token_path: Path
    workspaces: Path
    idle_ttl: float
    version: str

    def render(self, *, banner: bool, color: bool) -> str:
        """The startup block: wordmark + aligned facts on a tty, a flat
        line-per-fact preamble everywhere else (keeping the historical
        ``marim serve listening on …`` first line that scripts may grep)."""
        if not banner:
            head, *rest = self._fields(short=False)
            return "\n".join(
                [
                    f"marim serve {self._label} listening on {head[1]}",
                    *(f"{label}: {value}" for label, value in rest),
                ]
            )
        return "\n".join(
            [
                wordmark_block(f"serve {self._label}", color=color),
                "",
                field_block(self._fields(short=True), color=color),
                "",
            ]
        )

    @property
    def _label(self) -> str:
        """``v0.2.0`` for a real version, but no ``v`` in front of the
        ``unknown`` placeholder a source checkout reports."""
        return f"v{self.version}" if self.version[:1].isdigit() else self.version

    def _fields(self, *, short: bool) -> tuple[tuple[str, str], ...]:
        # Tilde-collapsed under the wordmark (a human is reading it), absolute in
        # the log preamble (a `~` in journald doesn't say whose home it was).
        show = _display_path if short else str
        return (
            ("listening", self.url),
            ("bearer token", show(self.token_path)),
            ("workspaces", show(self.workspaces)),
            ("idle ttl", f"{self.idle_ttl:g}s"),
        )


def main(argv: list[str], *, out=sys.stdout, err=sys.stderr) -> int:
    parser = argparse.ArgumentParser(
        prog="marim serve",
        description="Run the marim HTTP server daemon "
                    "(sessions over REST + WebSocket).",
    )
    parser.add_argument("--host", default="127.0.0.1",
                        help="bind address (default: 127.0.0.1)")
    parser.add_argument("--port", type=int, default=8642,
                        help="bind port (default: 8642)")
    parser.add_argument("--workspaces-root", type=Path, default=None,
                        help="directory for managed workspaces "
                             "(default: <state-dir>/workspaces)")
    parser.add_argument("--idle-ttl", type=float, default=900.0,
                        help="seconds before an idle session's harness is evicted "
                             "(default: 900)")
    parser.add_argument("--no-banner", action="store_true",
                        help="skip the startup wordmark (also: MARIM_NO_BANNER=1); "
                             "it is already skipped when stdout isn't a terminal")
    args = parser.parse_args(argv)

    try:
        import uvicorn

        from ...server.auth import load_or_create_token
        from ...server.http import create_app
        from ...server.supervisor import SessionSupervisor
        from ...server.workspaces import WorkspaceRegistry
    except ImportError:
        print(
            "marim serve requires the server extra. Install with:\n"
            "  uv add 'marim-harness[serve]'  (or: pip install 'marim-harness[serve]')",
            file=err,
        )
        return 1

    state_dir = _default_state_dir()
    token = load_or_create_token(state_dir)
    workspaces = args.workspaces_root or state_dir / "workspaces"
    registry = WorkspaceRegistry(state_dir / "workspaces.json", workspaces)
    supervisor = SessionSupervisor(idle_ttl=args.idle_ttl)
    app = create_app(registry=registry, supervisor=supervisor, token=token)

    startup = ServeStartup(
        url=f"http://{args.host}:{args.port}",
        token_path=state_dir / "token",
        workspaces=workspaces,
        idle_ttl=args.idle_ttl,
        version=package_version(),
    )
    # `out` is a StringIO under test and a pipe under systemd; both answer
    # isatty() honestly, which is exactly the signal we want.
    isatty = bool(getattr(out, "isatty", lambda: False)())
    print(
        startup.render(
            banner=banner_enabled(isatty=isatty, disabled=args.no_banner, env=os.environ),
            color=color_enabled(isatty=isatty, env=os.environ),
        ),
        file=out,
        flush=True,
    )
    uvicorn.run(app, host=args.host, port=args.port, log_level="warning")
    return 0
