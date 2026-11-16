import asyncio

import httpx
import pytest

from cinema.errors import Problem
from cinema.fluxer import Fluxer


async def test_discovers_split_endpoints_and_requires_bot(config):
    requests = []
    def remote(request):
        requests.append(request)
        if request.url.path == "/.well-known/fluxer":
            return httpx.Response(200, json={"endpoints": {"api_public": "https://rest.test/public",
                "gateway": "wss://socket.test/gw", "webapp": "https://app.test"}})
        return httpx.Response(200, json={"id": "bot", "bot": True})
    async with httpx.AsyncClient(transport=httpx.MockTransport(remote)) as http:
        fluxer = Fluxer(config, http)
        await fluxer.discover()
        assert fluxer.api == "https://rest.test/public/v1"
        assert str(requests[1].url) == "https://rest.test/public/v1/users/@me"
        assert "authorization" not in requests[0].headers
        assert requests[1].headers["authorization"] == "Bot bot-secret"


def test_voice_state_refresh_connection_ids_and_allowed_channels(config):
    config.fluxer_channel_ids = "a"
    fluxer = Fluxer(config, None)
    fluxer.dispatch("READY", {})
    assert fluxer.control_channel("u") is None
    fluxer.dispatch("GUILD_CREATE", {"id": "guild", "channels": [{"id": "a", "type": 2}, {"id": "b", "type": 2}],
        "voice_states": [{"user_id": "u", "channel_id": "a", "connection_id": "c"}]})
    assert fluxer.control_channel("u") == "a"
    fluxer.dispatch("VOICE_STATE_UPDATE", {"guild_id": "guild", "user_id": "u", "channel_id": "b", "connection_id": "c"})
    assert fluxer.control_channel("u") is None
    fluxer.dispatch("READY", {})
    assert not fluxer.states and not fluxer.ready


async def test_voice_grant_matches_channel_and_preserves_encryption(config, monkeypatch):
    fluxer = Fluxer(config, None)
    fluxer.online = fluxer.synced = True
    sent = []
    async def send(op, data):
        sent.append((op, data))
    monkeypatch.setattr(fluxer, "send", send)
    task = asyncio.create_task(fluxer.join("channel"))
    await asyncio.sleep(0)
    fluxer.dispatch("VOICE_SERVER_UPDATE", {"guild_id": "different", "channel_id": "channel", "token": "wrong"})
    assert not task.done()
    fluxer.dispatch("VOICE_SERVER_UPDATE", {"guild_id": "guild", "channel_id": "channel", "connection_id": "c",
        "endpoint": "wss://voice.test", "token": "grant", "e2ee_key": "encrypted"})
    assert (await task)["e2ee_key"] == "encrypted"
    await fluxer.advertise(True)
    assert sent[-1][1]["self_stream"] is True
    await fluxer.leave()
    assert sent[-1][1]["connection_id"] == "c" and sent[-1][1]["channel_id"] is None


async def test_rejects_user_account(config):
    def remote(request):
        if request.url.path.endswith("fluxer"):
            return httpx.Response(200, json={"endpoints": {"api_public": "https://rest.test", "gateway": "wss://gw.test", "webapp": "https://app.test"}})
        return httpx.Response(200, json={"id": "human", "bot": False})
    async with httpx.AsyncClient(transport=httpx.MockTransport(remote)) as http:
        with pytest.raises(Problem, match="bot account"):
            await Fluxer(config, http).discover()


async def test_presence_tracks_playback_clears_idle_and_refreshes(config, monkeypatch):
    fluxer = Fluxer(config, None)
    fluxer.online = fluxer.synced = True
    title = ["Movie"]
    fluxer.now_playing = lambda: title[0]
    sent = []
    sleeps = 0
    async def send(op, data):
        sent.append((op, data))
    async def sleep(_):
        nonlocal sleeps
        sleeps += 1
        if sleeps == 2:
            title[0] = None
        if sleeps == 4:
            title[0] = "Next movie"
        if sleeps == 5:
            raise asyncio.CancelledError
    monkeypatch.setattr(fluxer, "send", send)
    monkeypatch.setattr("cinema.fluxer.asyncio.sleep", sleep)
    with pytest.raises(asyncio.CancelledError):
        await fluxer.presence()
    assert [data["custom_status"] for _, data in sent] == [
        {"text": "Watching Movie"}, None, {"text": "Watching Next movie"}]
    assert all(op == 3 for op, _ in sent)
    # Each connection starts a fresh loop and republishes the current value.
    with pytest.raises(asyncio.CancelledError):
        sleeps = 4
        await fluxer.presence()
    assert sent[-1][1]["custom_status"] == {"text": "Watching Next movie"}
