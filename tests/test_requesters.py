import pytest

from cinema.config import Settings


@pytest.mark.parametrize("value", [None, "", "   ", " , , ", "456, 123, ,123"])
def test_requester_configuration(config, monkeypatch, value):
    monkeypatch.delenv("FLUXER_REQUESTER_IDS", raising=False)
    if value is not None:
        monkeypatch.setenv("FLUXER_REQUESTER_IDS", value)
    settings = Settings(_env_file=None, **config.model_dump(exclude={"fluxer_requester_ids"}))
    assert settings.can_request("123")
    assert settings.can_request("999") == (value != "456, 123, ,123")


@pytest.mark.parametrize("action", ["add", "play", "pause", "resume", "seek", "skip", "stop",
                                    "remove", "move", "volume", "tracks"])
async def test_read_only_user_cannot_mutate_even_in_voice(app, signed_in, config, action):
    config.fluxer_requester_ids = "456, 9123"
    fluxer = app.state.fluxer
    fluxer.online = fluxer.synced = True
    fluxer.channels = {"voice": {"type": 2}}
    fluxer.states = {"123": {"user_id": "123", "channel_id": "voice"}}
    before = app.state.playback.snapshot("123")
    response = await signed_in.post("/api/action", headers={"Origin": config.public_origin},
                                    json={"action": action})
    assert response.status_code == 403
    assert "read-only" in response.json()["error"]
    assert app.state.playback.snapshot("123") == before
    assert before["can_request"] is False
    assert before["can_control"] is False


async def test_read_only_user_can_browse_and_view_player(app, signed_in, config, monkeypatch):
    config.fluxer_requester_ids = "456"
    app.state.jellyfin.ready = True

    async def browse(*args):
        return {"items": [], "total": 0}

    monkeypatch.setattr(app.state.jellyfin, "browse", browse)
    assert (await signed_in.get("/api/me")).status_code == 200
    assert (await signed_in.get("/api/items")).status_code == 200
    response = await signed_in.get("/api/player")
    assert response.status_code == 200
    assert response.json()["can_request"] is False


@pytest.mark.parametrize("allowed", ["", "  ", "456, 123"])
async def test_allowed_requester_still_needs_voice(app, signed_in, config, allowed):
    config.fluxer_requester_ids = allowed
    fluxer = app.state.fluxer
    fluxer.online = fluxer.synced = True
    fluxer.channels = {"voice": {"type": 2}}
    command = {"action": "volume", "value": 42}
    response = await signed_in.post("/api/action", headers={"Origin": config.public_origin}, json=command)
    assert response.status_code == 403
    assert "voice channel" in response.json()["error"]
    fluxer.states = {"123": {"user_id": "123", "channel_id": "voice"}}
    response = await signed_in.post("/api/action", headers={"Origin": config.public_origin}, json=command)
    assert response.status_code == 200
    assert response.json()["volume"] == 42
    assert response.json()["can_request"] is True
    assert response.json()["can_control"] is True
