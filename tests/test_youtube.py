import asyncio
import json
from unittest.mock import AsyncMock

import pytest

from cinema.errors import Problem
from cinema.youtube import YouTube, canonical_url

VIDEO = "BaW_jenozKc"
URL = "https://www.youtube.com/watch?v=" + VIDEO
LIST = "PL1234567890"


@pytest.mark.parametrize("url", [URL + "&t=12", "https://youtu.be/" + VIDEO,
                                "https://www.youtube.com/shorts/" + VIDEO,
                                "https://music.youtube.com/watch?v=" + VIDEO])
def test_video_normalization(url):
    assert canonical_url(url) == URL


@pytest.mark.parametrize("url", [URL, "https://youtu.be/" + VIDEO,
                                "https://youtube.com/shorts/" + VIDEO])
def test_playlist_takes_precedence(url):
    assert canonical_url(url + ("&" if "?" in url else "?") + "list=" + LIST) == (
        "https://www.youtube.com/playlist?list=" + LIST)


@pytest.mark.parametrize("url", ["http://youtube.com/watch?v=" + VIDEO,
                                "https://youtube.com.evil.test/watch?v=" + VIDEO,
                                "https://user@youtube.com/watch?v=" + VIDEO,
                                "https://youtube.com:443/watch?v=" + VIDEO,
                                "file:///etc/passwd", "https://127.0.0.1/", "ytsearch:test",
                                "https://youtube.com/redirect?q=https://127.0.0.1",
                                "https://youtube.com/watch?v=bad"])
def test_unsafe_or_unsupported_urls(url):
    with pytest.raises(Problem):
        canonical_url(url)


async def test_playlist_metadata_is_allowlisted_and_ordered(monkeypatch):
    youtube = YouTube()
    monkeypatch.setattr(youtube, "extract", AsyncMock(return_value={"entries": [
        {"id": VIDEO, "title": "First", "url": "secret", "http_headers": {"Cookie": "secret"}},
        None, {"id": "abcdefghijk", "title": "Second"},
        {"id": "aaaaaaaaaaa", "availability": "private"}]}))
    rows = await youtube.items(URL)
    assert [row["title"] for row in rows] == ["First", "Second"]
    assert "secret" not in json.dumps(rows)


@pytest.mark.parametrize("entries", [[], [None], [{"id": VIDEO}] * 251])
async def test_empty_and_oversized_playlists(monkeypatch, entries):
    youtube = YouTube()
    monkeypatch.setattr(youtube, "extract", AsyncMock(return_value={"entries": entries}))
    with pytest.raises(Problem):
        await youtube.items(URL)


async def test_source_refresh_and_header_filtering(monkeypatch):
    youtube = YouTube()
    extract = AsyncMock(return_value={"url": "https://r1.googlevideo.com/videoplayback?signed=secret",
                                     "http_headers": {"User-Agent": "agent", "Cookie": "secret",
                                                      "Referer": "bad\r\nHeader: injected"}})
    monkeypatch.setattr(youtube, "extract", extract)
    for position in (0, 42):
        source = await youtube.resolve(VIDEO, position, None, -1)
        assert source.worker()["seek"] == position
        assert source.headers == {"User-Agent": "agent"}
    assert extract.await_count == 2


@pytest.mark.parametrize("data", [{"url": "file:///etc/passwd"}, {"url": "https://127.0.0.1/"},
                                 {"url": "https://googlevideo.com.evil.test/"}, {"is_live": True}])
async def test_bad_sources_and_live_videos(monkeypatch, data):
    youtube = YouTube()
    monkeypatch.setattr(youtube, "extract", AsyncMock(return_value=data))
    with pytest.raises(Problem):
        await youtube.resolve(VIDEO, 0, None, -1)


async def test_extractor_arguments_and_environment(monkeypatch):
    import cinema.youtube as module
    monkeypatch.setenv("FLUXER_BOT_TOKEN", "never-in-child")
    stdout = asyncio.StreamReader()
    stdout.feed_data(b'{"id":"BaW_jenozKc"}')
    stdout.feed_eof()
    process = AsyncMock(returncode=0, stdout=stdout)
    spawn = AsyncMock(return_value=process)
    monkeypatch.setattr(module.asyncio, "create_subprocess_exec", spawn)
    await YouTube().extract(URL, flat=True)
    args, kwargs = spawn.call_args
    assert args[-2:] == ("--", URL)
    assert "--ignore-config" in args and "--no-plugin-dirs" in args
    assert args[args.index("--playlist-end") + 1] == "251"
    assert "FLUXER_BOT_TOKEN" not in kwargs["env"]


async def test_extractor_cancellation_reaps_process(monkeypatch):
    import cinema.youtube as module
    stdout = asyncio.StreamReader()
    process = AsyncMock(returncode=None, stdout=stdout, pid=1234)
    monkeypatch.setattr(module.asyncio, "create_subprocess_exec", AsyncMock(return_value=process))
    killed = []
    if module.os.name != "nt":
        monkeypatch.setattr(module.os, "killpg", lambda *args: killed.append(args))
    else:
        process.kill = lambda: killed.append(True)
    task = asyncio.create_task(YouTube().extract(URL, flat=False))
    await asyncio.sleep(0)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert killed
    process.wait.assert_awaited_once()
