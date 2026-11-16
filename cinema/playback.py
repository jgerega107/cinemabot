import asyncio
import contextlib
import logging
import time
from typing import Literal
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field

from cinema.errors import Problem
from cinema.jellyfin import PLAYABLE, Jellyfin, Source
from cinema.supervisor import Supervisor

log = logging.getLogger(__name__)


class Action(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)
    action: Literal["add", "play", "pause", "resume", "seek", "skip", "stop", "remove", "move", "volume", "tracks"]
    item_id: str | None = Field(default=None, pattern=r"^[a-fA-F0-9-]{32,36}$")
    key: str | None = Field(default=None, max_length=36)
    position: float | None = Field(default=None, ge=0)
    index: int | None = Field(default=None, ge=0, le=249)
    value: int | None = Field(default=None, ge=0, le=100)
    audio: int | None = Field(default=None, ge=0, le=1000)
    subtitle: int = Field(default=-1, ge=-1, le=1000)


class Playback:
    def __init__(self, config, fluxer, jellyfin: Jellyfin, supervisor_factory=Supervisor):
        self.config, self.fluxer, self.jellyfin = config, fluxer, jellyfin
        self.worker = supervisor_factory(self.worker_event)
        self.lock = asyncio.Lock()
        self.queue: list[dict] = []
        self.current: dict | None = None
        self.source: Source | None = None
        self.channel: str | None = None
        self.state = "idle"
        self.position = 0.0
        self.volume = 70
        self.audio = None
        self.subtitle = -1
        self.error = ""
        self.generation = 0
        self.reconnect = False
        self.empty_since = None
        self.metrics = {}
        self.tasks: set[asyncio.Task] = set()
        self.closing = False
        self.events: asyncio.Queue = asyncio.Queue(maxsize=128)
        self.fluxer.now_playing = self.now_playing

    def now_playing(self) -> str | None:
        if not self.current or self.state != "playing" or self.reconnect:
            return None
        title = self.current["title"]
        series = (self.current.get("series") or "").strip()
        if self.current.get("type") == "Episode" and series:
            return f"{series} - {title}"
        return title

    def worker_event(self, message: dict) -> None:
        # Reader must never wait on the action lock: connect() waits on that same reader.
        if self.events.full():
            if message.get("event") == "progress":
                return
            self.events.get_nowait()
        self.events.put_nowait(message)

    def snapshot(self, user_id: str) -> dict:
        channel = self.fluxer.channels.get(self.channel, {})
        return {"current": self.current, "queue": self.queue, "state": self.state,
                "position": self.position, "volume": self.volume, "audio": self.audio,
                "subtitle": self.subtitle, "channel": self.channel,
                "channel_name": channel.get("name", self.channel),
                "can_control": bool(self.fluxer.control_channel(user_id, self.channel)),
                "available": self.fluxer.ready and self.jellyfin.ready,
                "error": self.error, "metrics": self.metrics}

    def authorize(self, user_id: str, channel: str | None = None) -> str:
        found = self.fluxer.control_channel(user_id, self.channel or channel)
        if not found or (channel and found != channel):
            raise Problem(403, "Join the active voice channel to control playback.")
        return found

    async def action(self, user: dict, command: Action):
        async with self.lock:
            requester = await self.fluxer.member(user["id"])
            channel = self.authorize(user["id"])
            action = command.action
            if action in {"add", "play"}:
                if not command.item_id:
                    raise Problem(400, "Choose a title.")
                if len(self.queue) >= 250:
                    raise Problem(409, "The queue is full.")
                item = await self.jellyfin.item(command.item_id)
                if item["type"] not in PLAYABLE:
                    raise Problem(400, "Choose a movie or episode.")
                self.authorize(user["id"], channel)
                row = {**item, "key": str(uuid4()), "requested_by": requester["name"],
                       "requester": {key: requester.get(key) for key in ("id", "name", "avatar")}}
                if action == "play" or not self.current:
                    await self._load(row, channel, user_id=user["id"])
                else:
                    self.queue.append(row)
            elif action == "stop":
                await self._stop(clear=True)
            elif action in {"remove", "move"}:
                row = next((r for r in self.queue if r["key"] == command.key), None)
                if not row:
                    raise Problem(409, "The queue has changed.")
                if action == "move" and command.index is None:
                    raise Problem(400, "Choose a queue position.")
                self.queue.remove(row)
                if action == "move":
                    self.queue.insert(min(command.index, len(self.queue)), row)
            elif action == "volume":
                if command.value is None:
                    raise Problem(400, "Choose a volume.")
                self.volume = command.value
                if self.worker.process:
                    await self.worker.send("volume", value=self.volume)
            else:
                if not self.current:
                    raise Problem(409, "Nothing is playing.")
                if command.key != self.current["key"]:
                    raise Problem(409, "The playing title changed. Try again.")
                if action == "skip":
                    await self._next()
                elif action == "pause":
                    if self.state not in {"playing", "loading", "paused"}:
                        raise Problem(409, "Playback is not active.")
                    await self.worker.send("pause")
                    self.state = "paused"
                    await self._report()
                elif action == "resume":
                    if self.reconnect or self.state == "error":
                        await self._load(self.current, channel, self.position, self.audio, self.subtitle,
                                         user_id=user["id"], force_connect=True)
                    else:
                        await self.worker.send("resume")
                        self.state = "playing"
                        await self._report()
                elif action in {"seek", "tracks"}:
                    position = command.position if action == "seek" else self.position
                    if position is None:
                        raise Problem(400, "Choose a seek position.")
                    audio = command.audio if action == "tracks" else self.audio
                    subtitle = command.subtitle if action == "tracks" else self.subtitle
                    await self._load(self.current, channel, position, audio, subtitle,
                                     paused=self.state == "paused", user_id=user["id"],
                                     force_connect=self.reconnect)

    async def _load(self, item: dict, channel: str, position: float = 0, audio: int | None = None,
                    subtitle: int = -1, paused: bool = False, user_id: str | None = None,
                    force_connect: bool = False):
        # Resolve before interrupting an existing title; invalid choices leave it untouched.
        source = await self.jellyfin.resolve(item["id"], position, audio, subtitle)
        try:
            if not self.fluxer.ready:
                raise Problem(503, "Fluxer disconnected while preparing playback.")
            if user_id:
                self.authorize(user_id, channel)
        except Exception:
            await self.jellyfin.release(source, position)
            raise
        try:
            self.generation += 1
            if self.source:
                with contextlib.suppress(Problem, OSError):
                    await self.worker.send("stop")
                await self.jellyfin.release(self.source, self.position)
                self.source = None
            if not self.worker.process or force_connect or self.channel != channel:
                await self.worker.close()
                grant = await self.fluxer.join(channel)
                await self.worker.connect(grant, self.config.video_bitrate, self.config.video_encoder)
                await self.worker.send("volume", value=self.volume)
            if user_id:
                self.authorize(user_id, channel)
            if not self.fluxer.ready:
                raise Problem(503, "Fluxer disconnected while joining voice.")
            self.channel, self.current, self.source = channel, item, source
            self.position, self.audio, self.subtitle = position, source.audio_index, subtitle
            self.error = ""
            self.metrics = {}
            self.reconnect = False
            self.state = "paused" if paused else "loading"
            await self.worker.send("play", source=source.worker(), generation=self.generation, paused=paused)
        except Exception:
            await self.jellyfin.release(source, position)
            self.source = None
            await self._fail("Could not start playback. Check the connections and selected tracks.")
            raise

    async def _report(self):
        if self.source:
            if self.metrics:
                log.info("Publisher performance: state=%s fps=%s queued_av_drift_ms=%s", self.state,
                         self.metrics.get("fps"), self.metrics.get("av_drift_ms"))
            try:
                await self.jellyfin.report(self.source, "progress", self.position, self.state == "paused")
            except Exception:
                log.warning("Jellyfin progress reporting unavailable")

    async def _next(self):
        if self.queue:
            row = self.queue.pop(0)
            try:
                await self._load(row, self.channel)
            except Exception:
                await self._fail("The next title could not be played.")
        else:
            await self._stop(clear=False)

    async def _stop(self, clear: bool):
        self.generation += 1
        await self.worker.close()
        if self.source:
            await self.jellyfin.release(self.source, self.position)
        await self.fluxer.leave()
        self.source = self.current = self.channel = None
        self.state = "idle"
        self.position = 0
        self.metrics = {}
        self.empty_since = None
        self.reconnect = False
        if clear:
            self.queue.clear()
            self.error = ""

    async def _fail(self, message: str):
        log.warning("Playback failed: %s", message)
        self.generation += 1
        self.error = message
        self.state = "error"
        self.reconnect = True
        await self.worker.close()
        if self.source:
            await self.jellyfin.release(self.source, self.position)
            self.source = None
        await self.fluxer.leave()

    async def handle_events(self):
        while True:
            message = await self.events.get()
            async with self.lock:
                if message.get("worker_epoch", getattr(self.worker, "epoch", 0)) != getattr(self.worker, "epoch", 0):
                    continue
                if message.get("generation", self.generation) != self.generation:
                    continue
                event = message["event"]
                try:
                    if event == "playing" and self.source:
                        await self.fluxer.advertise(True)
                        if self.state != "paused":
                            self.state = "playing"
                        await self.jellyfin.report(self.source, "start", self.position, self.state == "paused")
                        log.info("Playback started (%s, %sx%s)", self.source.method,
                                 message.get("width"), message.get("height"))
                    elif event == "progress" and self.current:
                        self.position = message["position"]
                        self.metrics = {k: message[k] for k in (
                            "fps", "av_drift_ms", "encoder_requested", "encoder_implementation",
                            "encoder_power_efficient") if k in message}
                    elif event == "ended":
                        if self.current:
                            self.position = message.get("position", self.position)
                            await self._next()
                    elif event in {"error", "fatal"}:
                        await self._fail(message.get("message", "Media worker failed."))
                    elif event == "disconnected" and self.current:
                        self.reconnect = True
                        self.state = "paused"
                        self.error = "Voice connection interrupted. Resume when connected again."
                except Exception:
                    await self._fail("Playback connection failed. Check Jellyfin and Fluxer.")

    async def monitor(self):
        last_report = 0
        while True:
            await asyncio.sleep(1)
            async with self.lock:
                if not self.channel:
                    continue
                if not self.fluxer.ready:
                    if self.current and not self.reconnect:
                        with contextlib.suppress(Exception):
                            await self.worker.send("pause")
                        self.reconnect = True
                        self.state = "paused"
                        self.error = "Fluxer disconnected. Resume after membership is synchronized."
                    self.empty_since = None
                    continue
                try:
                    occupied = await self.fluxer.human_present(self.channel)
                except Exception:
                    continue  # Uncertain membership never authorizes an action or starts an empty timer.
                if occupied:
                    self.empty_since = None
                elif self.empty_since is None:
                    self.empty_since = time.monotonic()
                elif time.monotonic() - self.empty_since >= self.config.empty_channel_seconds:
                    await self._stop(clear=True)
                    continue
                # A moderator move/kick must not leave media playing in another channel.
                connection = self.fluxer.connection_id
                if connection and self.state in {"playing", "paused"} and not self.reconnect:
                    voice = self.fluxer.states.get(connection)
                    if not voice or voice.get("channel_id") != self.channel:
                        await self._fail("The bot was moved or disconnected from its voice channel.")
                        continue
                if time.monotonic() - last_report >= 10:
                    await self._report()
                    last_report = time.monotonic()

    def start(self):
        self.tasks = {asyncio.create_task(self.handle_events()), asyncio.create_task(self.monitor())}

    async def close(self):
        self.closing = True
        for task in self.tasks:
            task.cancel()
        await asyncio.gather(*self.tasks, return_exceptions=True)
        async with self.lock:
            await self._stop(clear=True)
