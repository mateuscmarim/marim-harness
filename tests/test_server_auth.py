import stat

from marim_harness.server.auth import load_or_create_token, token_matches


def test_creates_token_with_0600_and_persists(tmp_path):
    state = tmp_path / "server"
    token = load_or_create_token(state)
    assert len(token) >= 32
    path = state / "token"
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert load_or_create_token(state) == token  # stable across calls


def test_token_matches():
    assert token_matches("secret", "secret")
    assert not token_matches("secret", "wrong")
    assert not token_matches("secret", None)
    assert not token_matches("secret", "")
