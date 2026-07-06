"""``marim serve`` — run marim as a long-lived HTTP daemon (REST + SSE).

Binds 127.0.0.1 by default; expose it via a reverse proxy or tailscale and
authenticate with the bearer token printed at startup (persisted 0600 under
the server state dir). Requires the ``serve`` extra (starlette + uvicorn)."""

import argparse
import os
import sys
from pathlib import Path


def _default_state_dir() -> Path:
    base = os.environ.get("XDG_DATA_HOME")
    root = Path(base) if base else Path.home() / ".local" / "share"
    return root / "marim-harness" / "server"


def main(argv: list[str], *, out=sys.stdout, err=sys.stderr) -> int:
    parser = argparse.ArgumentParser(
        prog="marim serve",
        description="Run the marim HTTP server daemon (sessions over REST + SSE).",
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
    registry = WorkspaceRegistry(
        state_dir / "workspaces.json",
        args.workspaces_root or state_dir / "workspaces",
    )
    supervisor = SessionSupervisor(idle_ttl=args.idle_ttl)
    app = create_app(registry=registry, supervisor=supervisor, token=token)

    print(f"marim serve listening on http://{args.host}:{args.port}", file=out)
    print(f"bearer token: {state_dir / 'token'}", file=out)
    uvicorn.run(app, host=args.host, port=args.port, log_level="warning")
    return 0
