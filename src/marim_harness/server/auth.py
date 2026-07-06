"""Single-owner bearer-token auth for the server daemon.

The token is generated once, stored 0600 under the server state dir, and
printed by ``marim serve`` at startup. Every request except /health must carry
it (Authorization: Bearer, or ?access_token= on the SSE endpoint, where
browser EventSource cannot set headers)."""

import secrets
from hmac import compare_digest
from pathlib import Path


def load_or_create_token(state_dir: Path) -> str:
    path = state_dir / "token"
    if path.exists():
        existing = path.read_text().strip()
        if existing:
            return existing
    state_dir.mkdir(parents=True, exist_ok=True)
    token = secrets.token_urlsafe(32)
    path.touch(mode=0o600, exist_ok=True)
    path.chmod(0o600)  # touch honors umask; force the mode we promised
    path.write_text(token + "\n")
    return token


def token_matches(expected: str, presented: str | None) -> bool:
    if not presented:
        return False
    return compare_digest(expected.encode(), presented.encode())
