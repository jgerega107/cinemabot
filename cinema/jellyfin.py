import asyncio
import contextlib
import logging
from dataclasses import dataclass
from urllib.parse import urlencode, urljoin, urlsplit
from uuid import UUID

import httpx

from cinema.config import Settings
from cinema.errors import Problem

TICKS = 10_000_000
PLAYABLE = {"Movie", "Episode"}
CATALOG = PLAYABLE | {"Series", "Season", "CollectionFolder"}
log = logging.getLogger(__name__)



def artwork_sources(item: dict) -> list[tuple[str, str]]:
    """Prefer a poster, then Jellyfin's available thumbnail/backdrop metadata."""
    sources = []
    tags = item.get("ImageTags") or {}
    if tags.get("Primary"):
        sources.append((item["Id"], "Primary"))
    if item.get("SeriesPrimaryImageTag") and item.get("SeriesId"):
        sources.append((item["SeriesId"], "Primary"))
    if tags.get("Thumb"):
        sources.append((item["Id"], "Thumb"))
    if item.get("BackdropImageTags"):
        sources.append((item["Id"], "Backdrop"))
    for kind in ("Thumb", "Backdrop"):
        if item.get(f"Parent{kind}ItemId") and (
            item.get(f"Parent{kind}ImageTag") or item.get(f"Parent{kind}ImageTags")
        ):
            sources.append((item[f"Parent{kind}ItemId"], kind))
    valid = []
    for source_id, kind in sources:
        try:
            UUID(source_id)
        except (ValueError, TypeError, AttributeError):
            continue
        if (source_id, kind) not in valid:
            valid.append((source_id, kind))
    return valid


def public_item(item: dict) -> dict:
    """Explicit allowlist: Jellyfin DTOs can contain paths, URLs, and tokens."""
    return {
        "id": item["Id"], "title": item.get("Name", "Untitled"), "type": item.get("Type"),
        "year": item.get("ProductionYear"), "overview": item.get("Overview", ""),
        "duration": (item.get("RunTimeTicks") or 0) / TICKS,
        "series": item.get("SeriesName"), "season": item.get("ParentIndexNumber"),
        "episode": item.get("IndexNumber"),
        "artwork": f"/api/artwork/{item['Id']}" if artwork_sources(item) else None,
        "audio": [{"index": s["Index"], "title": s.get("DisplayTitle") or s.get("Language") or "Audio"}
                  for s in item.get("MediaStreams", []) if s.get("Type") == "Audio"],
        "subtitles": [{"index": s["Index"], "title": s.get("DisplayTitle") or s.get("Language") or "Subtitles"}
                      for s in item.get("MediaStreams", []) if s.get("Type") == "Subtitle"],
    }


@dataclass
class Source:
    item_id: str
    media_source_id: str
    session_id: str
    url: str
    headers: dict
    offset: float
    seek: float
    audio_index: int | None
    subtitle_index: int
    method: str
    transcoded: bool

    def worker(self) -> dict:
        return {"url": self.url, "headers": self.headers, "offset": self.offset,
                "seek": self.seek, "audio_index": None if self.transcoded else self.audio_index}


class Jellyfin:
    def __init__(self, config: Settings, http: httpx.AsyncClient):
        self.config, self.http = config, http
        self.device_id = "jellyfin-fluxer-cinema"
        self.headers = {"Authorization": 'MediaBrowser Client="Fluxer Cinema", Device="Cinema", '
                        f'DeviceId="{self.device_id}", Version="0.1.0", '
                        f'Token="{config.jellyfin_access_token.get_secret_value()}"'}
        self.ready = False

    async def request(self, method: str, path: str, **kwargs):
        if path != "/Users/Me" and not self.ready:
            raise Problem(503, "Jellyfin account is not verified. Check the dedicated non-admin user credentials.")
        response = await self.http.request(method, self.config.jellyfin_url + path,
                                           headers=self.headers, **kwargs)
        if response.status_code in {401, 403}:
            self.ready = False
            raise Problem(503, "Jellyfin credentials do not allow this operation.")
        if response.status_code == 404:
            raise Problem(404, "This title is no longer available in the shared library.")
        if not response.is_success:
            raise Problem(503, "Jellyfin is unavailable or could not prepare this title.")
        return response.json() if response.content else None

    async def check(self) -> None:
        verified = False
        try:
            user = await self.request("GET", "/Users/Me")
            if user["Id"].replace("-", "") != self.config.jellyfin_user_id.replace("-", ""):
                raise Problem(503, "Jellyfin token and configured user ID do not match.")
            if user.get("Policy", {}).get("IsAdministrator"):
                raise Problem(503, "Use a dedicated non-admin Jellyfin account.")
            if not self.ready:
                log.info("Jellyfin connected; dedicated user verified")
            verified = True
        finally:
            self.ready = verified

    async def libraries(self) -> list:
        data = await self.request("GET", f"/Users/{self.config.jellyfin_user_id}/Views")
        return [public_item(x) for x in data.get("Items", []) if x.get("CollectionType") in {"movies", "tvshows"}]

    async def raw_item(self, item_id: str) -> dict:
        item = await self.request("GET", f"/Users/{self.config.jellyfin_user_id}/Items/{item_id}")
        if item.get("Type") not in CATALOG:
            raise Problem(400, "Only movies and TV are supported.")
        return item

    async def item(self, item_id: str) -> dict:
        return public_item(await self.raw_item(item_id))

    async def browse(self, query: str = "", parent: str | None = None, start: int = 0) -> dict:
        params = {"IncludeItemTypes": "Movie,Series", "Recursive": "true", "Limit": 40,
                  "StartIndex": start, "Fields": "Overview", "SortBy": "SortName", "SortOrder": "Ascending"}
        if parent:
            item = await self.raw_item(parent)
            params["ParentId"] = parent
            if item["Type"] in {"Series", "Season"}:
                params.update(IncludeItemTypes="Season" if item["Type"] == "Series" else "Episode",
                              Recursive="false", SortBy="IndexNumber")
        if query:
            params["SearchTerm"] = query
        result = await self.request("GET", f"/Users/{self.config.jellyfin_user_id}/Items", params=params)
        return {"items": [public_item(i) for i in result.get("Items", []) if i.get("Type") in CATALOG],
                "total": result.get("TotalRecordCount", 0), "start": start}

    async def artwork(self, item_id: str) -> tuple[bytes, str]:
        item = await self.raw_item(item_id)
        for source_id, kind in artwork_sources(item):
            if source_id != item_id:
                # Image endpoints alone do not enforce the selected user's library access.
                try:
                    await self.raw_item(source_id)
                except Problem as error:
                    if error.status == 404:
                        continue
                    raise
            path = f"/Items/{source_id}/Images/{kind}"
            async with self.http.stream("GET", self.config.jellyfin_url + path,
                                        headers=self.headers,
                                        params={"maxWidth": 500, "quality": 85, "format": "Webp"}) as r:
                if r.status_code == 404:
                    continue
                if not r.is_success:
                    raise Problem(404, "Artwork unavailable.")
                content = bytearray()
                async for chunk in r.aiter_bytes():
                    content.extend(chunk)
                    if len(content) > 4_000_000:
                        raise Problem(502, "Artwork exceeds the size limit.")
                content_type = r.headers.get("content-type", "").split(";")[0]
                if content_type not in {"image/webp", "image/jpeg", "image/png"}:
                    raise Problem(502, "Unexpected artwork format.")
                return bytes(content), content_type
        raise Problem(404, "Artwork unavailable.")

    def media_url(self, path: str) -> str:
        base = self.config.jellyfin_url
        parsed = urlsplit(path)
        if parsed.scheme or parsed.netloc:
            if (parsed.scheme, parsed.netloc) != (urlsplit(base).scheme, urlsplit(base).netloc):
                raise Problem(502, "Jellyfin returned a media URL outside its configured server.")
            return path
        prefix = urlsplit(base).path.rstrip("/")
        if prefix and path.startswith(prefix + "/"):
            return urljoin(base, path)
        return base + "/" + path.lstrip("/")

    async def resolve(self, item_id: str, offset: float, audio: int | None, subtitle: int) -> Source:
        item = await self.raw_item(item_id)
        if item["Type"] not in PLAYABLE:
            raise Problem(400, "Choose a movie or episode.")
        duration = (item.get("RunTimeTicks") or 0) / TICKS
        if offset < 0 or (duration and offset >= duration):
            raise Problem(400, "Seek position is outside the title.")
        streams = item.get("MediaStreams", [])
        for index, kind in ((audio, "Audio"), (subtitle, "Subtitle")):
            if index is not None and index >= 0 and not any(s.get("Index") == index and s.get("Type") == kind for s in streams):
                raise Problem(400, f"Selected {kind.lower()} track is unavailable.")
        hdr = any(s.get("VideoRangeType", "SDR") not in {None, "SDR", "Unknown"} or
                  s.get("VideoRange") == "HDR" for s in streams if s.get("Type") == "Video")
        transcode = subtitle >= 0 or hdr
        profile = {
            "Name": "Fluxer Cinema", "MaxStreamingBitrate": 200_000_000,
            "DirectPlayProfiles": [{"Container": "mkv,mp4,mov,webm,avi,ts,mpegts", "Type": "Video",
                                    "VideoCodec": "h264,hevc,av1,vp8,vp9,mpeg2video,mpeg4",
                                    "AudioCodec": "aac,ac3,eac3,dts,truehd,flac,opus,vorbis,mp3,pcm_s16le"}],
            "TranscodingProfiles": [{"Container": "ts", "Type": "Video", "VideoCodec": "h264",
                                     "AudioCodec": "aac", "Protocol": "hls", "Context": "Streaming",
                                     "MaxAudioChannels": "2", "MinSegments": 1, "BreakOnNonKeyFrames": False}],
            "CodecProfiles": [{"Type": "Video", "Codec": "h264", "Conditions": [
                {"Condition": "LessThanEqual", "Property": "Width", "Value": "1920", "IsRequired": False},
                {"Condition": "LessThanEqual", "Property": "Height", "Value": "1080", "IsRequired": False},
                {"Condition": "Equals", "Property": "VideoRangeType", "Value": "SDR", "IsRequired": True}]}],
            "SubtitleProfiles": [{"Format": fmt, "Method": "Encode"} for fmt in
                                 ("srt", "ass", "ssa", "subrip", "pgssub", "dvdsub", "vtt", "webvtt")],
        }
        body = {"UserId": self.config.jellyfin_user_id, "StartTimeTicks": int(offset * TICKS),
                "IsPlayback": True, "AutoOpenLiveStream": False,
                "EnableDirectPlay": not transcode, "EnableDirectStream": not transcode,
                "EnableTranscoding": True, "DeviceProfile": profile,
                "AudioStreamIndex": audio, "SubtitleStreamIndex": subtitle,
                "AlwaysBurnInSubtitleWhenTranscoding": True, "MaxAudioChannels": 2,
                "MaxStreamingBitrate": self.config.video_bitrate + 256_000 if transcode else 200_000_000}
        result = await self.request("POST", f"/Items/{item_id}/PlaybackInfo", json=body)
        if result.get("ErrorCode") or not result.get("MediaSources"):
            raise Problem(422, "Jellyfin cannot play this title with the selected tracks.")
        source = result["MediaSources"][0]
        # Some servers only attach stream characteristics to PlaybackInfo.
        source_hdr = any(s.get("VideoRange") == "HDR" or
                         s.get("VideoRangeType", "SDR") not in {None, "SDR", "Unknown"}
                         for s in source.get("MediaStreams", []) if s.get("Type") == "Video")
        if source_hdr and not transcode:
            body.update(EnableDirectPlay=False, EnableDirectStream=False,
                        MaxStreamingBitrate=self.config.video_bitrate + 256_000)
            result = await self.request("POST", f"/Items/{item_id}/PlaybackInfo", json=body)
            if result.get("ErrorCode") or not result.get("MediaSources"):
                raise Problem(422, "Jellyfin could not prepare an SDR version of this title.")
            source = result["MediaSources"][0]
            transcode = True
        if source.get("RequiresOpening"):
            raise Problem(422, "Live and externally opened media sources are not supported.")
        sid = source["Id"]
        audio = audio if audio is not None else source.get("DefaultAudioStreamIndex")
        direct = not transcode and (source.get("SupportsDirectPlay") or source.get("SupportsDirectStream"))
        if direct:
            path = f"/Videos/{item_id}/stream?" + urlencode({"Static": "true", "MediaSourceId": sid})
            method = "DirectPlay"
        else:
            path = source.get("TranscodingUrl")
            if not path:
                raise Problem(422, "Enable Jellyfin transcoding for this title or subtitle selection.")
            method = "Transcode"
        return Source(item_id, sid, result["PlaySessionId"], self.media_url(path), self.headers,
                      # Jellyfin VOD HLS covers the whole title, even when PlaybackInfo
                      # receives StartTimeTicks. The demuxer must seek within it.
                      offset, offset if direct or urlsplit(path).path.endswith(".m3u8") else 0,
                      audio, subtitle, method, not direct)

    async def report(self, source: Source, event: str, seconds: float, paused: bool = False) -> None:
        body = {"ItemId": source.item_id, "MediaSourceId": source.media_source_id,
                "PlaySessionId": source.session_id, "PositionTicks": int(seconds * TICKS),
                "IsPaused": paused, "CanSeek": True, "PlayMethod": source.method,
                "AudioStreamIndex": source.audio_index, "SubtitleStreamIndex": source.subtitle_index}
        path = {"start": "/Sessions/Playing", "progress": "/Sessions/Playing/Progress",
                "stop": "/Sessions/Playing/Stopped"}[event]
        await self.request("POST", path, json=body)

    async def release(self, source: Source, seconds: float) -> None:
        with contextlib.suppress(Exception):
            await self.report(source, "stop", seconds)
        if source.transcoded:
            with contextlib.suppress(Exception):
                await self.request("DELETE", "/Videos/ActiveEncodings", params={
                    "deviceId": self.device_id, "playSessionId": source.session_id})

    async def monitor(self) -> None:
        previous_error = None
        while True:
            try:
                await self.check()
                previous_error = None
            except Exception as error:
                self.ready = False
                reason = error.message if isinstance(error, Problem) else type(error).__name__
                if reason != previous_error:
                    log.warning("Jellyfin unavailable: %s", reason)
                    previous_error = reason
            await asyncio.sleep(30)
