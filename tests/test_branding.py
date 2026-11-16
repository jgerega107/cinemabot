
import httpx
import pytest

from cinema.branding import asset_url, branding
from cinema.fluxer import Fluxer


def test_branding_validates_assets_and_has_safe_defaults():
    brand = branding({"app_public": {"branding": {
        "product_name": " Gammy Land ", "theme_color": "#EC4899",
        "logo_url": "javascript:alert(1)", "icon_url": "/media/icon.png",
        "favicon_url": "https://cdn.test/icon.png",
    }}}, "https://chat.test")
    assert brand["name"] == "Gammy Land"
    assert brand["color"] == "#ec4899"
    assert brand["logo"] is None
    assert brand["icon"] == "https://chat.test/media/icon.png"
    assert branding({}, "https://chat.test")["name"] == "chat.test"
    for url in ("http://cdn.test/a", "https://user:pass@cdn.test/a", "data:image/png,test",
                "https://cdn.test/a;script-src", "https://[broken/icon.png"):
        assert asset_url(url, "https://chat.test") is None


@pytest.mark.parametrize("guild_avatar,expected", [
    ("guild-hash", "guilds/guild/users/123/avatars/guild-hash"),
    (None, "avatars/123/global-hash"),
])
async def test_requester_avatar_uses_discovered_media_and_community_profile(config, guild_avatar, expected):
    async def remote(request):
        return httpx.Response(200, json={"nick": "Community name", "avatar": guild_avatar,
                                        "user": {"id": "123", "username": "global", "avatar": "global-hash"}})
    async with httpx.AsyncClient(transport=httpx.MockTransport(remote)) as http:
        fluxer = Fluxer(config, http)
        fluxer.api = "https://api.test/v1"
        fluxer.media = "https://media.test/base"
        fluxer.online = fluxer.synced = True
        user = await fluxer.member("123")
        assert user == {"id": "123", "name": "Community name",
                        "avatar": "https://media.test/base/" + expected + ".webp?size=64"}


async def test_metadata_and_csp_allow_only_validated_asset_origins(app, client):
    app.state.fluxer.brand = branding({"app_public": {"branding": {
        "product_name": "Gammy Land", "theme_color": "#ec4899", "icon_url": "https://brand.test/icon.png",
    }}}, "https://chat.test")
    app.state.fluxer.media = "https://media.test/base"
    response = await client.get("/api/meta")
    assert response.json()["title"] == "Gammy Land Cinema"
    policy = response.headers["content-security-policy"]
    assert "https://brand.test" in policy and "https://media.test" in policy
    assert "unsafe-inline" not in policy
    response = await client.get("/api/me")
    assert response.json()["error"] == "Sign in with Gammy Land."

def test_malformed_optional_branding_does_not_break_discovery():
    for public in (None, [], {"branding": []}, {"branding": "invalid"}):
        assert branding({"app_public": public}, "https://chat.test")["name"] == "chat.test"
