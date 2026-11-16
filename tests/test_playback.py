import asyncio
import time

import pytest

from cinema.errors import Problem
from cinema.fluxer import Fluxer
from cinema.jellyfin import Source
from cinema.playback import Action, Playback

ID = "b" * 32
USER = {"id": "u", "name": "Alex"}


class Library:
    ready = True

    def __init__(self):
        self.released = []
        self.reports = []
        self.on_resolve = lambda: None

    async def item(self, item_id):
        return {"id": item_id, "title": "Movie", "type": "Movie", "duration": 120,
                "audio": [], "subtitles": [], "overview": ""}

    async def resolve(self, item_id, offset, audio, subtitle):
        self.on_resolve()
        return Source(item_id, "source", "session", "http://jellyfin/private", {}, offset, offset,
                      audio, subtitle, "DirectPlay", False)

    async def release(self, source, seconds):
        self.released.append((source, seconds))

    async def report(self, source, event, position, paused=False):
        self.reports.append((event, position, paused))


class Media:
    def __init__(self, callback):
        self.process = None
        self.commands = []

    async def connect(self, grant, bitrate):
        self.process = object()

    async def send(self, command, **payload):
        self.commands.append((command, payload))

    async def close(self):
        self.process = None


@pytest.fixture
def player(config, monkeypatch):
    fluxer = Fluxer(config, None)
    fluxer.online = fluxer.synced = True
    fluxer.channels = {"voice": {"id": "voice", "type": 2}}
    fluxer.states = {"u": {"user_id": "u", "channel_id": "voice"}}
    async def member(uid):
        return USER
    async def join(channel):
        return {"endpoint": "wss://voice.test", "token": "grant"}
    async def noop(*_):
        pass
    monkeypatch.setattr(fluxer, "member", member)
    monkeypatch.setattr(fluxer, "join", join)
    monkeypatch.setattr(fluxer, "advertise", noop)
    return Playback(config, fluxer, Library(), Media)


async def test_first_add_plays_and_other_adds_queue(player):
    await player.action(USER, Action(action="add", item_id=ID))
    key = player.current["key"]
    assert player.channel == "voice" and not player.queue
    await player.action(USER, Action(action="add", item_id=ID))
    assert player.current["key"] == key and len(player.queue) == 1
    assert "http://jellyfin" not in str(player.snapshot("u"))


async def test_leaving_voice_during_resolution_cannot_start_stream(player):
    player.jellyfin.on_resolve = player.fluxer.states.clear
    with pytest.raises(Problem, match="voice channel"):
        await player.action(USER, Action(action="play", item_id=ID))
    assert not player.worker.process
    assert not player.current
    assert player.jellyfin.released


async def test_lost_authorization_during_replacement_preserves_existing_source(player):
    await player.action(USER, Action(action="play", item_id=ID))
    existing, key = player.source, player.current["key"]
    player.jellyfin.on_resolve = player.fluxer.states.clear
    with pytest.raises(Problem):
        await player.action(USER, Action(action="play", item_id=ID))
    assert player.source is existing and player.current["key"] == key
    assert player.worker.process


async def test_every_mutation_checks_live_voice_membership(player):
    await player.action(USER, Action(action="play", item_id=ID))
    player.fluxer.states.clear()
    for action in (Action(action="stop"), Action(action="volume", value=5), Action(action="pause", key=player.current["key"])):
        with pytest.raises(Problem) as denied:
            await player.action(USER, action)
        assert denied.value.status == 403
    assert player.current


async def test_seek_cancels_old_generation_and_tracks_preserve_pause(player):
    await player.action(USER, Action(action="play", item_id=ID))
    key, generation = player.current["key"], player.generation
    await player.action(USER, Action(action="pause", key=key))
    await player.action(USER, Action(action="seek", key=key, position=25))
    assert player.state == "paused" and player.position == 25
    assert player.generation > generation
    assert player.worker.commands[-1][1]["paused"]
    task = asyncio.create_task(player.handle_events())
    player.worker_event({"event": "ended", "generation": generation})
    await asyncio.sleep(.01)
    task.cancel()
    await asyncio.gather(task, return_exceptions=True)
    assert player.current["key"] == key


async def test_stale_skip_cannot_skip_new_title(player):
    await player.action(USER, Action(action="play", item_id=ID))
    old = player.current["key"]
    await player.action(USER, Action(action="play", item_id=ID))
    with pytest.raises(Problem, match="changed"):
        await player.action(USER, Action(action="skip", key=old))
    assert player.current


async def test_serial_queue_mutations_and_stop_cleanup(player):
    await asyncio.gather(*(player.action(USER, Action(action="add", item_id=ID)) for _ in range(5)))
    assert len(player.queue) == 4
    await player.action(USER, Action(action="stop"))
    assert player.state == "idle" and not player.queue and not player.current
    assert not player.worker.process and player.jellyfin.released


async def test_worker_failure_keeps_retryable_title(player):
    await player.action(USER, Action(action="play", item_id=ID))
    key = player.current["key"]
    task = asyncio.create_task(player.handle_events())
    player.worker_event({"event": "error", "generation": player.generation, "message": "Decode failed"})
    await asyncio.sleep(.01)
    task.cancel()
    await asyncio.gather(task, return_exceptions=True)
    assert player.state == "error" and player.current["key"] == key
    assert not player.worker.process and player.source is None
    await player.action(USER, Action(action="resume", key=key))
    assert player.state == "loading" and player.worker.process


async def test_connection_loss_pauses_and_disables_controls(player):
    await player.action(USER, Action(action="play", item_id=ID))
    player.fluxer.online = False
    task = asyncio.create_task(player.monitor())
    await asyncio.sleep(1.05)
    task.cancel()
    await asyncio.gather(task, return_exceptions=True)
    assert player.state == "paused" and player.reconnect
    assert not player.snapshot("u")["can_control"]


async def test_restart_has_no_queue_or_playback(config):
    p = Playback(config, Fluxer(config, None), Library(), Media)
    assert p.queue == [] and p.current is None and p.state == "idle"


async def test_empty_channel_timeout_releases_everything(player):
    await player.action(USER, Action(action="play", item_id=ID))
    await player.action(USER, Action(action="add", item_id=ID))
    player.fluxer.states.clear()
    player.empty_since = time.monotonic() - 61
    task = asyncio.create_task(player.monitor())
    await asyncio.sleep(1.05)
    task.cancel()
    await asyncio.gather(task, return_exceptions=True)
    assert player.state == "idle" and not player.queue and not player.worker.process


async def test_requests_capture_verified_profile_and_preserve_it_when_queued(player, monkeypatch):
    profile = {"id": "u", "name": "Fresh nickname", "avatar": "https://media.test/avatar.webp"}
    async def member(uid):
        return profile
    monkeypatch.setattr(player.fluxer, "member", member)
    await player.action(USER, Action(action="add", item_id=ID))
    await player.action(USER, Action(action="add", item_id=ID))
    assert player.current["requested_by"] == "Fresh nickname"
    assert player.current["requester"] == profile
    assert player.queue[0]["requester"] == profile
    profile["name"] = "Changed later"
    assert player.current["requester"]["name"] == "Fresh nickname"


async def test_presence_only_exposes_actively_playing_title(player):
    player.current = {"title": "Movie"}
    for state in ("idle", "loading", "paused", "error"):
        player.state = state
        assert player.fluxer.now_playing() is None
    player.state = "playing"
    assert player.fluxer.now_playing() == "Movie"
    player.reconnect = True
    assert player.fluxer.now_playing() is None



@pytest.fixture
def youtube(player, monkeypatch):
    from unittest.mock import AsyncMock
    rows = [{"id": "BaW_jenozKc", "provider": "youtube", "type": "YouTube", "title": "Video",
             "duration": 60, "audio": [], "subtitles": [], "overview": ""},
            {"id": "abcdefghijk", "provider": "youtube", "type": "YouTube", "title": "Next video",
             "duration": 60, "audio": [], "subtitles": [], "overview": ""}]
    monkeypatch.setattr(player.youtube, "items", AsyncMock(return_value=rows))
    async def resolve(item_id, offset, audio, subtitle):
        return Source(item_id, "", "", "https://r1.googlevideo.com/private", {}, offset, offset,
                      None, -1, "YouTube", False)
    monkeypatch.setattr(player.youtube, "resolve", AsyncMock(side_effect=resolve))
    return player.youtube


async def test_youtube_playlist_and_mixed_queue(player, youtube):
    await player.action(USER, Action(action="add", url="https://youtu.be/BaW_jenozKc"))
    assert player.current["title"] == "Video" and len(player.queue) == 1
    assert player.queue[0]["requester"]["name"] == "Alex"
    await player._report()
    assert not player.jellyfin.reports
    await player.action(USER, Action(action="seek", key=player.current["key"], position=30))
    assert youtube.resolve.await_count == 2 and player.position == 30
    assert "googlevideo" not in str(player.snapshot("u"))
    await player.action(USER, Action(action="add", item_id=ID))
    await player.action(USER, Action(action="skip", key=player.current["key"]))
    assert player.current["title"] == "Next video"
    await player.action(USER, Action(action="skip", key=player.current["key"]))
    assert player.current["type"] == "Movie"
    assert not player.jellyfin.released
    await player.action(USER, Action(action="stop"))
    assert len(player.jellyfin.released) == 1


async def test_playlist_capacity_is_atomic(player, youtube):
    await player.action(USER, Action(action="play", item_id=ID))
    player.queue = [dict(player.current) for _ in range(249)]
    before = player.current
    with pytest.raises(Problem, match="room"):
        await player.action(USER, Action(action="add", url="https://youtu.be/BaW_jenozKc"))
    assert len(player.queue) == 249 and player.current is before
    youtube.resolve.assert_not_awaited()


async def test_youtube_authorization_rechecked_after_expansion(player, youtube):
    rows = youtube.items.return_value
    async def items(url):
        player.fluxer.states.clear()
        return rows
    youtube.items.side_effect = items
    with pytest.raises(Problem, match="voice channel"):
        await player.action(USER, Action(action="play", url="https://youtu.be/BaW_jenozKc"))
    assert player.current is None and not player.queue
    youtube.resolve.assert_not_awaited()


async def test_failed_queued_video_remains_retryable(player, youtube):
    await player.action(USER, Action(action="play", item_id=ID))
    await player.action(USER, Action(action="add", url="https://youtu.be/BaW_jenozKc"))
    failed_key = player.queue[0]["key"]
    player.audio, player.subtitle = 2, 3
    youtube.resolve.side_effect = Problem(502, "Unavailable")
    await player.action(USER, Action(action="skip", key=player.current["key"]))
    assert player.state == "error" and player.current["key"] == failed_key
    assert len(player.queue) == 1 and player.position == 0
    assert player.audio is None and player.subtitle == -1


async def test_ambiguous_request_rejected(player, youtube):
    with pytest.raises(Problem):
        await player.action(USER, Action(action="add", item_id=ID, url="https://youtu.be/BaW_jenozKc"))
    youtube.items.assert_not_awaited()


@pytest.mark.parametrize("position", [60, 61])
@pytest.mark.parametrize("state", ["playing", "paused"])
async def test_invalid_youtube_seek_preserves_playback(player, monkeypatch, position, state):
    from unittest.mock import AsyncMock

    # Exercise the real resolver; only extraction is stubbed. Queue metadata is
    # deliberately stale so validation must use the freshly extracted duration.
    monkeypatch.setattr(player.youtube, "extract", AsyncMock(return_value={
        "id": "BaW_jenozKc", "title": "Video", "duration": 60,
        "url": "https://r1.googlevideo.com/videoplayback"}))
    await player.action(USER, Action(action="play", url="https://youtu.be/BaW_jenozKc"))
    await player.action(USER, Action(action="add", item_id=ID))
    player.current["duration"] = 120
    player.state, player.position = state, 10
    before = player.snapshot("u")
    source, process, generation = player.source, player.worker.process, player.generation
    commands = list(player.worker.commands)

    with pytest.raises(Problem, match="Seek position is outside") as error:
        await player.action(USER, Action(action="seek", key=player.current["key"], position=position))

    assert error.value.status == 400
    assert player.snapshot("u") == before
    assert player.source is source and player.worker.process is process
    assert player.generation == generation and player.worker.commands == commands
    assert not player.jellyfin.released
