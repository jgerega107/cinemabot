import time
from urllib.parse import parse_qs, urlsplit

import httpx
import pytest

from cinema.errors import Problem


@pytest.mark.parametrize("route", ["/api/me", "/api/player", "/api/items", "/api/libraries",
                                   "/api/items/" + "a" * 32, "/api/artwork/" + "a" * 32, "/api/events"])
async def test_private_routes_require_login(client, route):
    response = await client.get(route)
    assert response.status_code == 401
    assert "jellyfin-secret" not in response.text


async def test_writes_require_origin_and_limit_body(signed_in):
    assert (await signed_in.post("/api/action", json={"action": "stop"})).status_code == 403
    assert (await signed_in.post("/api/action", headers={"Origin": "https://cinema.test"}, content="a" * 16385)).status_code == 413


async def test_expired_and_restart_sessions_fail(app, signed_in):
    app.state.auth.sessions["test-session"].expires = time.monotonic() - 1
    assert (await signed_in.get("/api/player")).status_code == 401
    assert "test-session" not in app.state.auth.sessions


async def test_membership_revocation_invalidates_session(app, signed_in, monkeypatch):
    async def revoked(uid):
        raise Problem(403, "Join the community.")
    monkeypatch.setattr(app.state.fluxer, "member", revoked)
    assert (await signed_in.get("/api/player")).status_code == 403
    assert not app.state.auth.sessions


async def test_oauth_pkce_state_replay_and_cookie(app, client):
    fluxer, auth = app.state.fluxer, app.state.auth
    fluxer.api, fluxer.webapp = "https://api.other.test/public/v1", "https://app.other.test"
    fluxer.online = fluxer.synced = True
    captured = []

    def upstream(request):
        captured.append(request)
        if request.url.path.endswith("/token"):
            return httpx.Response(200, json={"access_token": "private-oauth-token"})
        if request.url.path.endswith("/userinfo"):
            return httpx.Response(200, json={"id": "123"})
        if request.url.path.endswith("/members/123"):
            return httpx.Response(200, json={"user": {"id": "123", "username": "Alex"}})
        return httpx.Response(404)

    async with httpx.AsyncClient(transport=httpx.MockTransport(upstream)) as remote:
        auth.http = fluxer.http = remote
        response = await client.get("/auth/login")
        assert response.status_code == 303
        url = urlsplit(response.headers["location"])
        assert url.netloc == "app.other.test"
        params = parse_qs(url.query)
        assert params["scope"] == ["identify"] and params["code_challenge_method"] == ["S256"]
        callback = "/auth/callback?code=one-use-code&state=" + params["state"][0]
        result = await client.get(callback)
        assert result.status_code == 303
        cookie = result.headers.get_list("set-cookie")[-1]
        assert all(x in cookie for x in ("Secure", "HttpOnly", "SameSite=lax", "Path=/"))
        assert "private-oauth-token" not in str(auth.sessions)
        assert (await client.get(callback)).status_code == 400
        body = parse_qs(captured[0].content.decode())
        assert body["code_verifier"] and body["client_secret"] == ["client-secret"]


async def test_oauth_state_mismatch_never_exchanges_code(app, client):
    app.state.fluxer.api = "https://api.test/v1"
    app.state.fluxer.webapp = "https://web.test"
    await client.get("/auth/login")
    assert (await client.get("/auth/callback?state=wrong&code=secret")).status_code == 400


async def test_session_cookie_never_authorizes_voice_action(app, signed_in):
    result = await signed_in.post("/api/action", headers={"Origin": "https://cinema.test"}, json={"action": "stop"})
    assert result.status_code == 403


async def test_sse_checks_expiration(app, signed_in):
    app.state.auth.sessions["test-session"].expires = time.monotonic() + .1
    response = await signed_in.get("/api/events")
    assert "event: player" in response.text
    assert '"status": 401' in response.text


async def test_health_independent_of_upstreams(client):
    assert (await client.get("/healthz")).status_code == 200
    result = await client.get("/readyz")
    assert result.status_code == 503
    assert result.json() == {"ok": False, "fluxer": False, "jellyfin": False}
