import json
from urllib.parse import parse_qs, urlsplit

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
    assert source.seek == 10
    assert source.worker()["seek"] == source.worker()["offset"] == 10
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


@pytest.mark.parametrize("metadata", [
    {"ImageTags": {"Thumb": "thumb"}},
    {"BackdropImageTags": ["backdrop"]},
    {"SeriesId": "c" * 32, "SeriesPrimaryImageTag": "poster"},
    {"ParentBackdropItemId": "c" * 32, "ParentBackdropImageTags": ["backdrop"]},
])
def test_metadata_exposes_artwork_for_jellyfin_fallbacks(metadata):
    assert public_item({**ITEM, **metadata})["artwork"] == f"/api/artwork/{ID}"


async def test_missing_primary_falls_back_to_authorized_series_poster(config):
    series_id = "c" * 32
    requests = []
    def remote(request):
        path = request.url.path
        requests.append(path)
        if "/Images/" in path:
            if f"/{ID}/" in path:
                return httpx.Response(404)
            return httpx.Response(200, content=b"poster", headers={"content-type": "image/webp"})
        if path.endswith(series_id):
            return httpx.Response(200, json={"Id": series_id, "Type": "Series"})
        return httpx.Response(200, json={**ITEM, "ImageTags": {"Primary": "stale"},
                                        "SeriesId": series_id, "SeriesPrimaryImageTag": "poster"})
    async with httpx.AsyncClient(transport=httpx.MockTransport(remote)) as http:
        jf = Jellyfin(config, http)
        jf.ready = True
        assert await jf.artwork(ID) == (b"poster", "image/webp")
    assert requests[-2:] == [f"/Users/{config.jellyfin_user_id}/Items/{series_id}",
                             f"/Items/{series_id}/Images/Primary"]


async def test_inaccessible_parent_artwork_is_not_fetched(config):
    series_id = "c" * 32
    def remote(request):
        assert "/Images/" not in request.url.path
        if request.url.path.endswith(series_id):
            return httpx.Response(404)
        return httpx.Response(200, json={**ITEM, "SeriesId": series_id,
                                        "SeriesPrimaryImageTag": "poster"})
    async with httpx.AsyncClient(transport=httpx.MockTransport(remote)) as http:
        jf = Jellyfin(config, http)
        jf.ready = True
        with pytest.raises(Problem, match="Artwork unavailable"):
            await jf.artwork(ID)


@pytest.mark.parametrize("audio,subtitle,expected", [(None, -1, 1), (3, -1, 3), (3, 2, 3), (1, -1, 1)])
async def test_transcoded_track_switch_overrides_stale_url_defaults(config, audio, subtitle, expected):
    item = {**ITEM, "MediaStreams": [*ITEM["MediaStreams"], {"Type": "Audio", "Index": 3}]}
    def remote(request):
        if "PlaybackInfo" in request.url.path:
            return httpx.Response(200, json={"PlaySessionId": "new-session", "MediaSources": [{
                "Id": "source", "DefaultAudioStreamIndex": 1, "MediaStreams": item["MediaStreams"],
                "SupportsDirectPlay": False, "TranscodingUrl": (
                    "/Videos/test/master.m3u8?AudioStreamIndex=1&audiostreamindex=1"
                    "&SubtitleStreamIndex=2&SubtitleMethod=External&PlaySessionId=old-session"
                    "&DeviceId=old-device&VideoCodec=h264&api_key=private-key")}]})
        return httpx.Response(200, json=item)
    async with httpx.AsyncClient(transport=httpx.MockTransport(remote)) as http:
        jf = Jellyfin(config, http)
        jf.ready = True
        src = await jf.resolve(ID, 10, audio, subtitle)
    query = {key.lower(): value for key, value in parse_qs(urlsplit(src.url).query).items()}
    assert query["audiostreamindex"] == [str(expected)]
    assert query["subtitlestreamindex"] == [str(subtitle)]
    assert query["playsessionid"] == ["new-session"]
    assert query["deviceid"] == [jf.device_id]
    assert query["videocodec"] == ["h264"] and query["api_key"] == ["private-key"]
    assert query.get("subtitlemethod") == (["Encode"] if subtitle >= 0 else None)
    assert src.audio_index == expected and src.worker()["audio_index"] is None
    assert src.seek == src.offset == 10


@pytest.mark.parametrize("selected,expected,ordinal", [(None, 2, 0), (2, 2, 0), (3, 3, 1), (4, 4, 2)])
async def test_direct_audio_uses_audio_order_not_global_jellyfin_index(config, selected, expected, ordinal):
    streams = [{"Type": "Subtitle", "Index": 0}, {"Type": "Video", "Index": 1},
               {"Type": "Audio", "Index": 2, "IsDefault": True},
               {"Type": "Audio", "Index": 3}, {"Type": "Audio", "Index": 4}]
    def remote(request):
        if "PlaybackInfo" in request.url.path:
            return httpx.Response(200, json={"PlaySessionId": "session", "MediaSources": [{
                "Id": "source", "DefaultAudioStreamIndex": 2, "SupportsDirectPlay": True,
                "MediaStreams": streams}]})
        return httpx.Response(200, json={**ITEM, "MediaStreams": streams})
    async with httpx.AsyncClient(transport=httpx.MockTransport(remote)) as http:
        jf = Jellyfin(config, http)
        jf.ready = True
        src = await jf.resolve(ID, 10, selected, -1)
    assert src.audio_index == expected
    assert src.worker()["audio_ordinal"] == ordinal
    assert not src.transcoded
