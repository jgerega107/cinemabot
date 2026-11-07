import time

import httpx
import pytest

from cinema.app import create_app
from cinema.auth import Session
from cinema.config import Settings


@pytest.fixture
def config():
    return Settings(_env_file=None, public_origin="https://cinema.test", fluxer_origin="https://chat.test",
                    fluxer_guild_id="guild", fluxer_bot_token="bot-secret", fluxer_client_id="client",
                    fluxer_client_secret="client-secret", jellyfin_url="http://jellyfin:8096",
                    jellyfin_user_id="a" * 32, jellyfin_access_token="jellyfin-secret")


@pytest.fixture
async def app(config):
    app = create_app(config, start_services=False)
    async with app.router.lifespan_context(app):
        yield app


@pytest.fixture
async def client(app):
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="https://cinema.test") as client:
        yield client


@pytest.fixture
def signed_in(app, client, monkeypatch):
    async def member(uid):
        return {"id": uid, "name": "Alex"}
    monkeypatch.setattr(app.state.fluxer, "member", member)
    app.state.auth.sessions["test-session"] = Session({"id": "123", "name": "Alex"}, time.monotonic() + 100, time.monotonic())
    client.cookies.set("__Host-cinema", "test-session")
    return client
