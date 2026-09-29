from types import SimpleNamespace
from urllib.parse import parse_qs, urlsplit

import pytest
from aiohttp import web

from bot.cogs.resource_library import ResourceLibraryCog


class FakeBot:
    application_id = 1234


def _local_request(**kwargs):
    return SimpleNamespace(
        remote=kwargs.get("remote", "127.0.0.1"),
        host=kwargs.get("host", "127.0.0.1:8080"),
        query=kwargs.get("query", {}),
        cookies=kwargs.get("cookies", {}),
    )


def _enable_standalone(monkeypatch):
    monkeypatch.setenv("RESOURCE_STANDALONE_ENABLED", "1")
    monkeypatch.setenv("RESOURCE_STANDALONE_GUILD_ID", "5001")
    monkeypatch.setenv(
        "RESOURCE_STANDALONE_REDIRECT_URI",
        "http://127.0.0.1:8080/api/auth/standalone/callback",
    )
    monkeypatch.setenv("RESOURCE_WEB_PORT", "8080")


def test_standalone_mode_is_disabled_by_default(monkeypatch):
    monkeypatch.delenv("RESOURCE_STANDALONE_ENABLED", raising=False)
    assert ResourceLibraryCog._standalone_enabled() is False


def test_standalone_configuration_requires_loopback_binding(monkeypatch):
    _enable_standalone(monkeypatch)
    cog = ResourceLibraryCog(FakeBot())

    cog._validate_standalone_configuration("127.0.0.1", 8080)
    with pytest.raises(RuntimeError, match="RESOURCE_WEB_HOST=127.0.0.1"):
        cog._validate_standalone_configuration("0.0.0.0", 8080)


def test_standalone_configuration_requires_matching_local_oauth_callback(monkeypatch):
    _enable_standalone(monkeypatch)
    monkeypatch.setenv(
        "RESOURCE_STANDALONE_REDIRECT_URI",
        "https://attacker.example/callback",
    )
    cog = ResourceLibraryCog(FakeBot())

    with pytest.raises(RuntimeError, match="RESOURCE_STANDALONE_REDIRECT_URI"):
        cog._validate_standalone_configuration("127.0.0.1", 8080)


def test_standalone_api_is_loopback_only_and_guild_allowlisted(monkeypatch):
    _enable_standalone(monkeypatch)
    cog = ResourceLibraryCog(FakeBot())

    cog._enforce_api_guild(_local_request(), 5001)
    with pytest.raises(web.HTTPForbidden) as guild_error:
        cog._enforce_api_guild(_local_request(), 9001)
    assert "測試伺服器" in guild_error.value.text
    with pytest.raises(web.HTTPForbidden) as remote_error:
        cog._enforce_api_guild(_local_request(remote="192.0.2.15"), 5001)
    assert "本機" in remote_error.value.text
    with pytest.raises(web.HTTPForbidden) as host_error:
        cog._enforce_api_guild(_local_request(host="example.test:8080"), 5001)
    assert "本機" in host_error.value.text


def test_standalone_routes_are_hidden_when_mode_is_disabled(monkeypatch):
    monkeypatch.delenv("RESOURCE_STANDALONE_ENABLED", raising=False)
    cog = ResourceLibraryCog(FakeBot())

    with pytest.raises(web.HTTPNotFound) as error:
        cog._require_standalone_request(_local_request())
    assert "未啟用" in error.value.text


@pytest.mark.asyncio
async def test_standalone_login_redirect_sets_csrf_state_cookie(monkeypatch):
    _enable_standalone(monkeypatch)
    monkeypatch.setenv("DISCORD_CLIENT_ID", "1234")
    monkeypatch.setenv("DISCORD_CLIENT_SECRET", "server-secret")
    cog = ResourceLibraryCog(FakeBot())

    response = await cog._api_standalone_login(_local_request())

    assert response.status == 302
    location = urlsplit(response.headers["Location"])
    assert location.netloc == "discord.com"
    query = parse_qs(location.query)
    assert query["client_id"] == ["1234"]
    assert query["redirect_uri"] == [
        "http://127.0.0.1:8080/api/auth/standalone/callback"
    ]
    assert query["scope"] == ["identify"]
    assert query["state"]
    cookie = response.cookies["resource_standalone_oauth_state"]
    assert cookie["httponly"]
    assert cookie["samesite"] == "Lax"


@pytest.mark.asyncio
async def test_standalone_callback_rejects_oauth_state_mismatch(monkeypatch):
    _enable_standalone(monkeypatch)
    cog = ResourceLibraryCog(FakeBot())
    request = _local_request(
        query={"state": "attacker-state", "code": "authorization-code"},
        cookies={"resource_standalone_oauth_state": "expected-state"},
    )

    response = await cog._api_standalone_callback(request)

    assert response.status == 302
    assert response.headers["Location"] == "/activity?standalone_error=invalid_state"
    assert "resource_standalone_oauth_state" in response.cookies
