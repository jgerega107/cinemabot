import json

import httpx
import pytest

from cinema.errors import Problem
from cinema.jellyfin import Jellyfin, public_item

ID = "b" * 32
ITEM = {"Id": ID, "Name": "A film", "Type": "Movie", "RunTimeTicks": 1200_000_000,
        "Path": "/private/media/movie.mkv", "MediaSources": [{"Path": "secret-url"}],
        "MediaStreams": [{"Type": "Audio", "Index": 1}, {"Type": "Subtitle", "Index": 2}]}


def test_public_metadata_excludes_paths_credentials():
    result = public_item(ITEM)
    assert "Path" not in result and "MediaSources" not in result
    assert result["duration"] == 120 and result["audio"][0]["index"] == 1


@pytest.mark.parametrize("subtitle,transcode", [(-1, False), (2, True)])
async def test_playback_negotiation_and_private_urls(config, subtitle, transcode):
    requests = []
    def remote(request):
        requests.append(request)
        if "PlaybackInfo" in request.url.path:
            return httpx.Response(200, json={"PlaySessionId": "play", "MediaSources": [{"Id": "source",
                "DefaultAudioStreamIndex": 1, "SupportsDirectPlay": True,
                "TranscodingUrl": f"/Videos/{ID}/master.m3u8?MediaSourceId=source"}]})
        return httpx.Response(200, json=ITEM)
    async with httpx.AsyncClient(transport=httpx.MockTransport(remote)) as http:
        jellyfin = Jellyfin(config, http)
        jellyfin.ready = True
        source = await jellyfin.resolve(ID, 10, 1, subtitle)
    body = json.loads(requests[-1].content)
    assert body["EnableDirectPlay"] is not transcode
    assert source.transcoded is transcode
    assert source.seek == (0 if transcode else 10)
    assert source.offset == 10
    assert source.url.startswith("http://jellyfin:8096/")
    assert "jellyfin-secret" not in source.url
    assert source.worker()["audio_index"] == (None if transcode else 1)


async def test_disallowed_or_missing_tracks_rejected_before_playback(config):
    async with httpx.AsyncClient(transport=httpx.MockTransport(lambda r: httpx.Response(200, json=ITEM))) as http:
        jellyfin = Jellyfin(config, http)
        jellyfin.ready = True
        with pytest.raises(Problem, match="audio"):
            await jellyfin.resolve(ID, 0, 200, -1)
        with pytest.raises(Problem, match="Seek"):
            await jellyfin.resolve(ID, 500, 1, -1)


def test_base_paths_and_external_sources(config):
    config.jellyfin_url = "https://internal.test/jellyfin"
    jf = Jellyfin(config, None)
    assert jf.media_url("/Videos/test") == "https://internal.test/jellyfin/Videos/test"
    assert jf.media_url("/jellyfin/Videos/test") == "https://internal.test/jellyfin/Videos/test"
    with pytest.raises(Problem):
        jf.media_url("https://attacker.test/media")
    with pytest.raises(Problem):
        jf.media_url("//attacker.test/media")


@pytest.mark.parametrize("admin,uid,message", [(True, "a" * 32, "non-admin"), (False, "b" * 32, "do not match")])
async def test_admin_tokens_and_mismatched_users_rejected(config, admin, uid, message):
    async with httpx.AsyncClient(transport=httpx.MockTransport(lambda r: httpx.Response(200, json={
            "Id": uid, "Policy": {"IsAdministrator": admin}}))) as http:
        jf = Jellyfin(config, http)
        jf.ready = True  # A previously accepted account may be changed by an administrator.
        with pytest.raises(Problem, match=message):
            await jf.check()
        assert not jf.ready
        with pytest.raises(Problem, match="not verified"):
            await jf.libraries()


async def test_catalog_and_playback_wait_for_account_verification(config):
    requests = []
    def upstream(request):
        requests.append(request.url.path)
        if request.url.path == "/Users/Me":
            return httpx.Response(200, json={"Id": config.jellyfin_user_id, "Policy": {"IsAdministrator": False}})
        return httpx.Response(200, json={"Items": []})
    async with httpx.AsyncClient(transport=httpx.MockTransport(upstream)) as http:
        jf = Jellyfin(config, http)
        with pytest.raises(Problem, match="not verified"):
            await jf.libraries()
        with pytest.raises(Problem, match="not verified"):
            await jf.resolve(ID, 0, None, -1)
        assert not requests
        await jf.check()
        assert await jf.libraries() == []
        assert len(requests) == 2


async def test_hdr_reported_only_by_playback_info_forces_transcode(config):
    bodies = []
    def remote(request):
        if "PlaybackInfo" in request.url.path:
            bodies.append(json.loads(request.content))
            return httpx.Response(200, json={"PlaySessionId": "session", "MediaSources": [{"Id": "source",
                "SupportsDirectPlay": True, "TranscodingUrl": "/Videos/test/master.m3u8",
                "MediaStreams": [{"Type": "Video", "VideoRangeType": "HDR10"}]}]})
        return httpx.Response(200, json=ITEM)
    async with httpx.AsyncClient(transport=httpx.MockTransport(remote)) as http:
        jellyfin = Jellyfin(config, http)
        jellyfin.ready = True
        source = await jellyfin.resolve(ID, 0, None, -1)
    assert len(bodies) == 2 and bodies[-1]["EnableDirectPlay"] is False
    assert source.transcoded and "m3u8" in source.url
