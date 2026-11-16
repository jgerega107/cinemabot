"""Bounded YouTube extraction; signed media URLs never enter public queue data."""
import asyncio
import contextlib
import json
import os
import re
import signal
import sys
import tempfile
from urllib.parse import parse_qs, urlsplit

import deno

from cinema.errors import Problem
from cinema.jellyfin import Source

VIDEO_ID = re.compile(r"[A-Za-z0-9_-]{11}\Z")
PLAYLIST_ID = re.compile(r"[A-Za-z0-9_-]{10,100}\Z")


def canonical_url(value: str, playlist: bool = True) -> str:
    try:
        url = urlsplit(value.strip())
        if (url.scheme != "https" or url.username or url.password or url.port
                or url.hostname not in {"youtube.com", "www.youtube.com", "m.youtube.com",
                                        "music.youtube.com", "youtu.be"}):
            raise ValueError
        query = parse_qs(url.query)
        if playlist and query.get("list") and (
                url.path in {"/watch", "/playlist"} or url.hostname == "youtu.be"
                or url.path.startswith(("/shorts/", "/embed/", "/live/"))):
            identifier = query["list"][0]
            if PLAYLIST_ID.fullmatch(identifier):
                return "https://www.youtube.com/playlist?list=" + identifier
        if url.hostname == "youtu.be":
            identifier = url.path.lstrip("/")
        elif url.path == "/watch":
            identifier = query.get("v", [""])[0]
        elif url.path.startswith(("/shorts/", "/embed/", "/live/")):
            identifier = url.path.split("/")[-1]
        else:
            raise ValueError
        if VIDEO_ID.fullmatch(identifier):
            return "https://www.youtube.com/watch?v=" + identifier
    except ValueError:
        pass
    raise Problem(400, "Enter an HTTPS YouTube video or playlist link.")


class YouTube:
    def __init__(self, *, enabled: bool = True):
        self.enabled = enabled

    def require_enabled(self):
        if not self.enabled:
            raise Problem(403, "YouTube link requests are disabled by the administrator.")

    async def extract(self, url: str, *, flat: bool) -> dict:
        # No shell, inherited credentials, user configuration, plugins, or downloads.
        env = {k: v for k, v in os.environ.items() if k in {
            "PATH", "SYSTEMROOT", "WINDIR", "TEMP", "TMP", "TMPDIR", "LANG", "SSL_CERT_FILE"}}
        scratch = tempfile.TemporaryDirectory(prefix="cinema-youtube-")
        env.update(HOME=scratch.name, DENO_DIR=scratch.name, TMPDIR=scratch.name,
                   TEMP=scratch.name, TMP=scratch.name)
        args = [sys.executable, "-m", "yt_dlp", "--ignore-config", "--no-plugin-dirs",
                "--js-runtimes", "deno:" + deno.find_deno_bin(),
                "--no-cache-dir", "--no-warnings", "--skip-download", "--dump-single-json",
                "--socket-timeout", "10", "--retries", "1", "--extractor-retries", "1"]
        if flat:
            args += ["--flat-playlist", "--playlist-end", "251", "--ignore-errors"]
        else:
            # One muxed stream works with the existing decoder without disk downloads/merging.
            args += ["--no-playlist", "--format", "best[height<=1080][vcodec!=none][acodec!=none]"]
        process = None
        try:
            process = await asyncio.create_subprocess_exec(
                *args, "--", url, stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.DEVNULL, env=env, start_new_session=os.name != "nt")
            async with asyncio.timeout(45):
                output = bytearray()
                while chunk := await process.stdout.read(65536):
                    output.extend(chunk)
                    if len(output) > 8 * 1024 * 1024:
                        raise ValueError
                await process.wait()
                if process.returncode:
                    raise ValueError
                result = json.loads(output)
                if not isinstance(result, dict):
                    raise ValueError
                return result
        except (TimeoutError, ValueError, OSError):
            raise Problem(502, "YouTube could not load this link. It may be unavailable or restricted.") from None
        finally:
            if process and process.returncode is None:
                with contextlib.suppress(ProcessLookupError):
                    if os.name != "nt":
                        os.killpg(process.pid, signal.SIGKILL)
                    else:
                        process.kill()
                await process.wait()
            scratch.cleanup()

    async def items(self, url: str) -> list[dict]:
        self.require_enabled()
        info = await self.extract(canonical_url(url), flat=True)
        entries = info.get("entries") if "entries" in info else [info]
        if not isinstance(entries, list) or len(entries) > 250:
            raise Problem(409, "Playlists must contain at most 250 videos.")
        rows = []
        for entry in entries:
            if not entry or entry.get("availability") in {"private", "premium_only", "subscriber_only"}:
                continue
            identifier = entry.get("id", "")
            if not VIDEO_ID.fullmatch(identifier):
                continue
            rows.append({"id": identifier, "provider": "youtube", "type": "YouTube",
                         "title": str(entry.get("title") or "YouTube video")[:300],
                         "duration": entry.get("duration") or 0, "overview": "",
                         "audio": [], "subtitles": []})
        if not rows:
            raise Problem(400, "This link has no available YouTube videos.")
        return rows

    async def resolve(self, item_id: str, position: float, audio, subtitle: int) -> Source:
        self.require_enabled()
        if audio is not None or subtitle != -1:
            raise Problem(400, "YouTube uses its default audio and does not support subtitle selection.")
        info = await self.extract(canonical_url("https://www.youtube.com/watch?v=" + item_id), flat=False)
        if info.get("is_live") or info.get("live_status") in {"is_live", "is_upcoming"}:
            raise Problem(400, "Live and upcoming YouTube streams are not supported yet.")
        duration = info.get("duration") or 0
        if position < 0 or (duration and position >= duration):
            raise Problem(400, "Seek position is outside the title.")
        url = info.get("url", "")
        parsed = urlsplit(url)
        if (parsed.scheme != "https" or not (parsed.hostname or "").endswith(".googlevideo.com")
                or parsed.username or parsed.password or parsed.port):
            raise Problem(502, "YouTube did not return a supported media stream.")
        headers = {k: v for k, v in info.get("http_headers", {}).items()
                   if k.lower() in {"user-agent", "referer", "origin"}
                   and isinstance(v, str) and "\r" not in v and "\n" not in v}
        return Source(item_id, "", "", url, headers, position, position, None, -1, "YouTube", False)
