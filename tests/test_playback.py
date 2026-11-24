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
        self.episodes = []
        self.on_resolve = lambda: None

    async def item(self, item_id):
        episode = next((episode for episode in self.episodes if episode["id"] == item_id), None)
        if episode:
            return episode
        return {"id": item_id, "title": "Movie", "type": "Movie", "duration": 120,
                "audio": [{"index": 1, "title": "English"}],
                "subtitles": [{"index": 2, "title": "English"}], "overview": ""}

    async def following_episodes(self, item, limit):
        start = self.episodes.index(next(episode for episode in self.episodes if episode["id"] == item["id"])) + 1
        return self.episodes[start:start + limit]

    async def resolve(self, item_id, offset, audio, subtitle):
        self.on_resolve()
        return Source(item_id, "source", "session", "http://jellyfin/private", {}, offset, offset,
                      audio, subtitle, "DirectPlay", False)

    async def release(self, source, seconds):
        self.released.append((source, seconds))

    async def report(self, source, event, position, paused=False):
        self.reports.append((event, position, paused))


def episode(number, audio, subtitles):
    return {"id": f"{number:032x}", "title": f"Episode {number}", "type": "Episode", "duration": 120,
            "series_id": "a" * 32, "series": "Example Show", "audio": audio, "subtitles": subtitles}


def track(index, language, title=None, *, forced=False):
    return {"index": index, "language": language, "title": title or language, "forced": forced}


class Media:
    def __init__(self, callback):
        self.process = None
        self.commands = []

    async def connect(self, grant, bitrate, video_encoder="auto"):
        self.video_encoder = video_encoder
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
    player.config.video_encoder = "nvenc"
    await player.action(USER, Action(action="add", item_id=ID))
    assert player.worker.video_encoder == "nvenc"
    key = player.current["key"]
    assert player.channel == "voice" and not player.queue
    await player.action(USER, Action(action="add", item_id=ID))
    assert player.current["key"] == key and len(player.queue) == 1
    assert "http://jellyfin" not in str(player.snapshot("u"))


async def test_queue_validates_and_uses_selected_tracks_on_playback(player):
    await player.action(USER, Action(action="add", item_id=ID))
    with pytest.raises(Problem, match="Selected subtitle track"):
        await player.action(USER, Action(action="add", item_id=ID, audio=1, subtitle=50))
    assert not player.queue
    await player.action(USER, Action(action="add", item_id=ID, audio=1, subtitle=2))
    assert player.queue[0]["track_selection"] == {"audio": 1, "subtitle": 2}
    await player.action(USER, Action(action="skip", key=player.current["key"]))
    assert (player.audio, player.subtitle) == (1, 2)


async def test_episodes_continue_with_matching_tracks_and_recover_after_missing_tracks(player):
    episode_one = episode(1, [track(1, "jpn")], [track(2, "eng")])
    episode_two = episode(2, [track(1, "eng"), track(4, "jpn")],
                          [track(2, "eng", forced=True), track(5, "eng")])
    episode_three = episode(3, [track(1, "eng")], [track(2, "eng", forced=True)])
    episode_four = episode(4, [track(3, "jpn")], [track(7, "eng")])
    episode_five = episode(5, [], [])
    player.jellyfin.episodes = [episode_one, episode_two, episode_three, episode_four, episode_five]
    await player.action(USER, Action(action="add", item_id=episode_one["id"], audio=1, subtitle=2))
    assert [row["id"] for row in player.queue] == [episode_two["id"], episode_three["id"], episode_four["id"]]
    assert player.preferred_tracks(episode_two) == {"audio": 4, "subtitle": 5}

    # Normal completion advances the queue and replenishes its lookahead.
    task = asyncio.create_task(player.handle_events())
    try:
        player.worker_event({"event": "ended", "generation": player.generation})
        await asyncio.sleep(0)
        assert player.current["id"] == episode_two["id"]
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
    assert (player.audio, player.subtitle) == (4, 5)
    assert [row["id"] for row in player.queue] == [episode_three["id"], episode_four["id"], episode_five["id"]]
    await player.action(USER, Action(action="skip", key=player.current["key"]))
    assert (player.audio, player.subtitle) == (None, -1)
    await player.action(USER, Action(action="skip", key=player.current["key"]))
    assert (player.audio, player.subtitle) == (3, 7)


async def test_removed_episodes_stay_removed_and_manual_queue_choices_take_precedence(player):
    first = episode(1, [track(1, "jpn")], [track(2, "eng")])
    second = episode(2, [track(1, "eng"), track(3, "jpn")], [track(4, "eng")])
    third = episode(3, [], [])
    fourth = episode(4, [track(5, "jpn")], [track(6, "eng")])
    player.jellyfin.episodes = [first, second, third, fourth]
    await player.action(USER, Action(action="add", item_id=ID))
    await player.action(USER, Action(action="add", item_id=first["id"], audio=1, subtitle=2))
    assert [row["id"] for row in player.queue] == [first["id"]]

    await player.action(USER, Action(action="skip", key=player.current["key"]))
    original_key = player.queue[0]["key"]
    await player.action(USER, Action(action="add", item_id=second["id"], audio=1))
    assert player.queue[0]["key"] == original_key
    await player.action(USER, Action(action="remove", key=player.queue[1]["key"]))
    await player.action(USER, Action(action="skip", key=player.current["key"]))
    assert (player.audio, player.subtitle) == (1, 4)
    assert [row["id"] for row in player.queue] == [fourth["id"]]

    await player.action(USER, Action(action="tracks", key=player.current["key"], audio=3))
    assert player.subtitle == 4
    await player.action(USER, Action(action="tracks", key=player.current["key"], subtitle=-1))
    assert player.audio == 3
    await player.action(USER, Action(action="skip", key=player.current["key"]))
    assert (player.audio, player.subtitle) == (5, -1)


async def test_episode_lookup_failure_does_not_interrupt_playback(player, monkeypatch):
    first = episode(1, [], [])
    player.jellyfin.episodes = [first]

    async def unavailable(*_):
        raise Problem(503, "Series unavailable.")

    monkeypatch.setattr(player.jellyfin, "following_episodes", unavailable)
    await player.action(USER, Action(action="play", item_id=first["id"]))
    assert player.current["id"] == first["id"]
    assert player.worker.process and not player.queue


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
    for subtitle in (2, -1):
        await player.action(USER, Action(action="tracks", key=key, audio=1, subtitle=subtitle))
        assert player.state == "paused" and player.position == 25
        assert player.source.seek == player.source.offset == 25
        assert player.subtitle == subtitle
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
    player.current = {"type": "Episode", "title": "Pilot", "series": "Example Show"}
    assert player.fluxer.now_playing() == "Example Show - Pilot"
    player.current["series"] = None
    assert player.fluxer.now_playing() == "Pilot"
    player.current = {"type": "Movie", "title": "Movie", "series": "Collection"}
    assert player.fluxer.now_playing() == "Movie"
    player.reconnect = True
    assert player.fluxer.now_playing() is None
