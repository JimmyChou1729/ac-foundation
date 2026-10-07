import httpx


def test_distribution_supports_inherited_socks5h_without_network(monkeypatch):
    monkeypatch.setenv("ALL_PROXY", "socks5h://127.0.0.1:1")
    monkeypatch.setenv("all_proxy", "socks5h://127.0.0.1:1")
    with httpx.Client() as client:
        assert client.trust_env is True
