from runtime.config import Settings


def test_deerflow_internal_auth_token_uses_shared_env_name(monkeypatch):
    monkeypatch.delenv("DEERFLOW_INTERNAL_AUTH_TOKEN", raising=False)
    monkeypatch.setenv("DEER_FLOW_INTERNAL_AUTH_TOKEN", "shared-token")

    assert Settings(_env_file=None).deerflow_internal_auth_token == "shared-token"


def test_deerflow_internal_auth_token_does_not_accept_old_env_name(monkeypatch):
    monkeypatch.delenv("DEER_FLOW_INTERNAL_AUTH_TOKEN", raising=False)
    monkeypatch.setenv("DEERFLOW_INTERNAL_AUTH_TOKEN", "old-token")

    assert Settings(_env_file=None).deerflow_internal_auth_token is None
