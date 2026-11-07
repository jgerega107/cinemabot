import asyncio
import ipaddress
from urllib.parse import parse_qs, urlsplit

import httpx
import pytest
from starlette.requests import Request

from cinema.errors import Problem
from cinema.security import Limits, ResponseCache, client_address, reject_brotli


async def test_login_flood_is_throttled_before_shared_state_allocation(app, client):
    app.state.fluxer.api, app.state.fluxer.webapp = "https://api.test/v1", "https://chat.test"
    responses = [await client.get("/auth/login") for _ in range(11)]
    assert [r.status_code for r in responses[:10]] == [303] * 10
    assert responses[-1].status_code == 429
    assert responses[-1].headers["retry-after"]
    assert not app.state.auth.used_flows
    other = httpx.ASGITransport(app=app, client=("198.51.100.2", 12345))
    async with httpx.AsyncClient(transport=other, base_url="https://cinema.test") as visitor:
        assert (await visitor.get("/auth/login")).status_code == 303


async def test_stateless_login_tampering_expiry_and_restart(app, client, monkeypatch):
    auth = app.state.auth
    app.state.fluxer.api, app.state.fluxer.webapp = "https://api.test/v1", "https://chat.test"
    response = await client.get("/auth/login")
    token = client.cookies.get(auth.flow_cookie)
    state = parse_qs(urlsplit(response.headers["location"]).query)["state"][0]
    assert auth.flow(token)
    assert auth.flow(token[:-1] + ("A" if token[-1] != "A" else "B")) is None
    assert auth.flow("é" * 20) is None
    assert (await client.get("/auth/callback", params={"state": "é", "code": "test"})).status_code == 400
    auth.flow_secret = b"a different process after restart"
    assert auth.flow(token) is None
    assert (await client.get("/auth/callback", params={"state": state, "code": "test"})).status_code == 400
    response = await client.get("/auth/login")
    token = client.cookies.get(auth.flow_cookie)
    until = int(token.split(".")[1])
    monkeypatch.setattr("cinema.auth.time.time", lambda: until + 1)
    assert auth.flow(token) is None


async def test_action_limit_precedes_membership_and_survives_new_sessions(app, signed_in, monkeypatch):
    calls = []
    async def member(uid):
        calls.append(uid)
        return {"id": uid, "name": "Alex"}
    monkeypatch.setattr(app.state.fluxer, "member", member)
    app.state.fluxer.online = app.state.fluxer.synced = True
    app.state.fluxer.channels = {"voice": {"type": 2}}
    app.state.fluxer.states = {"123": {"user_id": "123", "channel_id": "voice"}}
    for _ in range(10):
        response = await signed_in.post("/api/action", headers={"Origin": "https://cinema.test"},
                                        json={"action": "volume", "value": 50})
        assert response.status_code == 200
    before = len(calls)
    app.state.auth.sessions["new-session"] = app.state.auth.sessions["test-session"]
    signed_in.cookies.clear()
    signed_in.cookies.set("__Host-cinema", "new-session")
    response = await signed_in.post("/api/action", headers={"Origin": "https://cinema.test"},
                                    json={"action": "volume", "value": 50})
    assert response.status_code == 429 and len(calls) == before


async def test_catalog_cache_reauthorizes_and_read_limit_stops_upstreams(app, signed_in, monkeypatch):
    membership, fetches = [], []
    removed = False
    async def member(uid):
        membership.append(uid)
        if removed:
            raise Problem(403, "Membership removed")
        return {"id": uid, "name": "Alex"}
    async def browse(*args):
        fetches.append(args)
        return {"items": [], "total": 0}
    monkeypatch.setattr(app.state.fluxer, "member", member)
    monkeypatch.setattr(app.state.jellyfin, "browse", browse)
    app.state.jellyfin.ready = True
    for _ in range(80):
        assert (await signed_in.get("/api/items")).status_code == 200
    assert len(fetches) == 1
    before = len(membership)
    assert (await signed_in.get("/api/items")).status_code == 429
    assert len(membership) == before
    app.state.limits.buckets.clear()
    removed = True
    assert (await signed_in.get("/api/items")).status_code == 403
    assert len(fetches) == 1


async def test_artwork_reuses_bounded_cache(app, signed_in, monkeypatch):
    calls = []
    async def artwork(item):
        calls.append(item)
        return b"image", "image/webp"
    app.state.jellyfin.ready = True
    monkeypatch.setattr(app.state.jellyfin, "artwork", artwork)
    path = "/api/artwork/" + "a" * 32
    assert (await signed_in.get(path)).content == b"image"
    assert (await signed_in.get(path)).content == b"image"
    assert len(calls) == 1
    app.state.jellyfin.ready = False
    assert (await signed_in.get(path)).status_code == 503


async def test_concurrency_rejects_before_another_membership_lookup(app, signed_in, monkeypatch):
    entered, release = asyncio.Event(), asyncio.Event()
    calls = []
    async def member(uid):
        calls.append(uid)
        entered.set()
        await release.wait()
        return {"id": uid, "name": "Alex"}
    monkeypatch.setattr(app.state.fluxer, "member", member)
    async def action():
        return await signed_in.post("/api/action", headers={"Origin": "https://cinema.test"},
                                    json={"action": "stop"})
    task = asyncio.create_task(action())
    await entered.wait()
    assert (await action()).status_code == 429
    assert len(calls) == 1
    release.set()
    await task
    assert not app.state.limits.active


def test_untrusted_forwarded_headers_cannot_reset_ip_limits():
    def request(peer, header):
        return Request({"type": "http", "client": (peer, 1), "headers": [(b"x-forwarded-for", header.encode())]})
    trusted = [ipaddress.ip_network("10.0.0.1/32")]
    assert client_address(request("192.0.2.1", "198.51.100.1"), trusted) == "192.0.2.1"
    assert client_address(request("10.0.0.1", "203.0.113.9, 198.51.100.1"), trusted) == "198.51.100.1"
    assert client_address(request("10.0.0.1", "invalid"), trusted) == "10.0.0.1"
    assert client_address(request("2001:db8::abcd", ""), []) == "2001:db8::/64"


def test_rate_limits_refill_and_storage_is_bounded(monkeypatch):
    limits = Limits(max_keys=2)
    clock = 0
    monkeypatch.setattr("cinema.security.time.monotonic", lambda: clock)
    limits.rate("a", per_minute=60, burst=1)
    with pytest.raises(Problem):
        limits.rate("a", per_minute=60, burst=1)
    clock = 1
    limits.rate("a", per_minute=60, burst=1)
    for key in ("b", "c", "d"):
        limits.rate(key, per_minute=60, burst=1)
    assert len(limits.buckets) == 2


async def test_cache_coalesces_fetches_expires_and_evicts():
    cache = ResponseCache(max_bytes=6, ttl=10, size=len, max_entries=2)
    calls = []
    async def fetch():
        calls.append(1)
        await asyncio.sleep(.01)
        return b"data"
    assert await asyncio.gather(cache.get("a", fetch), cache.get("a", fetch)) == [b"data"] * 2
    assert len(calls) == 1
    await cache.get("b", fetch)
    assert list(cache.entries) == ["b"] and cache.bytes == 4
    cache.entries["b"] = (0, b"data", 4)
    await cache.get("b", fetch)
    assert len(calls) == 3
    await cache.close()


async def test_brotli_rejected_before_decoder_or_body_read(client):
    assert (await client.post("/auth/logout", headers={"Origin": "https://cinema.test", "Content-Encoding": "br"},
                               content=b"test")).status_code == 415
    class Unreadable(httpx.AsyncByteStream):
        async def __aiter__(self):
            raise AssertionError("Brotli body must not be read or decoded")
            yield b""
    async with httpx.AsyncClient(transport=httpx.MockTransport(lambda _: httpx.Response(
            200, headers={"Content-Encoding": "br"}, stream=Unreadable())),
            event_hooks={"response": [reject_brotli]}) as http:
        with pytest.raises(Problem, match="unsupported"):
            await http.get("https://upstream.test")
