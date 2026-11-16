import asyncio
import contextlib
import json
import logging
import time
from collections.abc import Callable
from urllib.parse import parse_qsl, quote, urlencode, urlsplit, urlunsplit

import httpx
import websockets

from cinema.branding import asset_url, branding
from cinema.config import Settings
from cinema.errors import Problem

log = logging.getLogger(__name__)


def endpoint(value: str, schemes: set[str]) -> str:
    p = urlsplit(value)
    if p.scheme not in schemes or not p.hostname or p.username or p.password or p.fragment:
        raise Problem(503, "Fluxer returned an invalid service endpoint.")
    return value.rstrip("/")


class Fluxer:
    def __init__(self, config: Settings, http: httpx.AsyncClient):
        self.config, self.http = config, http
        self.api = self.webapp = self.gateway = self.media = ""
        self.brand = branding({}, config.fluxer_origin)
        self.user_id = ""
        self.online = self.synced = False
        self.states: dict[str, dict] = {}
        self.channels: dict[str, dict] = {}
        self.bots: set[str] = set()
        self.humans: dict[str, float] = {}
        self.connection_id: str | None = None
        self.channel_id: str | None = None
        self.socket = None
        self.pending: asyncio.Future | None = None
        self.on_change: Callable[[], None] = lambda: None
        self.now_playing: Callable[[], str | None] = lambda: None
        self._closing = False
        self._acknowledged = True

    @property
    def ready(self) -> bool:
        return self.online and self.synced

    async def discover(self) -> None:
        response = await self.http.get(self.config.fluxer_origin + "/.well-known/fluxer")
        response.raise_for_status()
        document = response.json()
        endpoints = document["endpoints"]
        self.brand = branding(document, self.config.fluxer_origin)
        self.media = asset_url(endpoints.get("media"), self.config.fluxer_origin) or ""
        schemes = {"https", "http"} if self.config.allow_insecure_local else {"https"}
        self.api = endpoint(endpoints.get("api_public") or endpoints["api"], schemes)
        if not self.api.endswith("/v1"):
            self.api += "/v1"
        self.webapp = endpoint(endpoints["webapp"], schemes)
        self.gateway = endpoint(endpoints["gateway"], {"wss", "ws"} if self.config.allow_insecure_local else {"wss"})
        me = await self.request("GET", "/users/@me")
        if not me.get("bot"):
            raise Problem(503, "FLUXER_BOT_TOKEN must belong to a bot account.")
        self.user_id = me["id"]
        self.bots.add(self.user_id)

    async def request(self, method: str, path: str, **kwargs):
        for attempt in range(4):
            response = await self.http.request(method, self.api + path, headers={
                "Authorization": "Bot " + self.config.fluxer_bot_token.get_secret_value(),
            }, **kwargs)
            if response.status_code == 429 and attempt < 3:
                await asyncio.sleep(min(30, max(1, float(response.json().get("retry_after", 1)))))
                continue
            if response.status_code == 404:
                raise Problem(404, "Fluxer community or member was not found.")
            if not response.is_success:
                raise Problem(503, "Fluxer request failed. Check bot permissions and instance availability.")
            return response.json() if response.content else None
        raise Problem(503, "Fluxer rate limit exceeded.")

    async def member(self, user_id: str) -> dict:
        if not self.ready:
            raise Problem(503, "Fluxer membership is being synchronized.")
        try:
            member = await self.request("GET", f"/guilds/{self.config.fluxer_guild_id}/members/{user_id}")
        except Problem as e:
            if e.status == 404:
                raise Problem(403, "Join the configured Fluxer community to use this player.") from None
            raise
        if member.get("user", {}).get("bot"):
            self.bots.add(user_id)
        else:
            self.humans[user_id] = time.monotonic() + 30
        user = member.get("user", {})
        avatar_hash = member.get("avatar") or user.get("avatar")
        avatar = None
        if self.media and isinstance(avatar_hash, str) and avatar_hash:
            uid = quote(user_id, safe="")
            path = (f"guilds/{quote(self.config.fluxer_guild_id, safe='')}/users/{uid}/avatars/"
                    if member.get("avatar") else f"avatars/{uid}/")
            avatar = self.media.rstrip("/") + "/" + path + quote(avatar_hash, safe="") + ".webp?size=64"
        return {"id": user_id, "name": member.get("nick") or user.get("global_name")
                or user.get("username") or "Member", "avatar": avatar}

    def control_channel(self, user_id: str, active: str | None = None) -> str | None:
        if not self.ready:
            return None
        choices = sorted({v["channel_id"] for v in self.states.values() if v.get("user_id") == user_id
                          and v.get("channel_id") in self.channels
                          and self.channels[v["channel_id"]].get("type") == 2
                          and (not self.config.channel_ids or v["channel_id"] in self.config.channel_ids)})
        return (active if active in choices else None) if active else next(iter(choices), None)

    async def human_present(self, channel: str) -> bool:
        ids = {v["user_id"] for v in self.states.values() if v.get("channel_id") == channel}
        for user_id in ids - self.bots:
            # Query unknown participants rather than treating every non-self account as human.
            if self.humans.get(user_id, 0) > time.monotonic():
                return True
            await self.member(user_id)
            if user_id not in self.bots:
                return True
        return False

    async def send(self, op: int, data) -> None:
        if self.socket is None:
            raise Problem(503, "Fluxer Gateway is disconnected.")
        await self.socket.send(json.dumps({"op": op, "d": data}))

    async def join(self, channel: str) -> dict:
        await self.leave()
        if not self.ready:
            raise Problem(503, "Fluxer Gateway is not ready.")
        self.channel_id = channel
        self.pending = asyncio.get_running_loop().create_future()
        try:
            await self.send(4, {"guild_id": self.config.fluxer_guild_id, "channel_id": channel,
                                "self_mute": False, "self_deaf": True, "self_video": False,
                                "self_stream": False, "p2p": False})
            grant = await asyncio.wait_for(self.pending, 15)
            self.connection_id = grant.get("connection_id")
            if not self.connection_id or not grant.get("endpoint") or not grant.get("token") or grant.get("p2p"):
                raise Problem(503, "This voice channel did not supply a supported LiveKit grant.")
            endpoint(grant["endpoint"], {"wss", "ws"} if self.config.allow_insecure_local else {"wss"})
            return {k: grant[k] for k in ("endpoint", "token", "e2ee_key") if grant.get(k)}
        except TimeoutError:
            raise Problem(503, "Voice join timed out. Check View Channel, Connect, and Stream permissions.") from None
        finally:
            self.pending = None

    async def advertise(self, streaming: bool) -> None:
        if self.connection_id:
            await self.send(4, {"guild_id": self.config.fluxer_guild_id, "channel_id": self.channel_id,
                                "connection_id": self.connection_id, "self_stream": streaming,
                                "self_mute": False, "self_deaf": True, "self_video": False})

    async def leave(self) -> None:
        if self.connection_id:
            with contextlib.suppress(Exception):
                await self.send(4, {"guild_id": self.config.fluxer_guild_id,
                                    "channel_id": None, "connection_id": self.connection_id})
        self.connection_id = self.channel_id = None

    def dispatch(self, event: str, data: dict) -> None:
        was_ready = self.ready
        if event == "READY":
            self.online = True
            # Always re-identify on reconnect: never authorize against an old voice snapshot.
            self.synced = False
            self.states.clear()
            for guild in data.get("guilds", []):
                if guild.get("id") == self.config.fluxer_guild_id and "voice_states" in guild:
                    self.dispatch("GUILD_CREATE", guild)
        elif event in {"GUILD_CREATE", "GUILD_SYNC"} and data.get("id") == self.config.fluxer_guild_id:
            self.channels = {c["id"]: c for c in data.get("channels", [])}
            self.states.clear()
            for member in data.get("members", []):
                if member.get("user", {}).get("bot"):
                    self.bots.add(member["user"]["id"])
            for voice in data.get("voice_states", []):
                self._voice(voice)
            self.synced = not data.get("unavailable", False)
        elif event == "GUILD_DELETE" and data.get("id") == self.config.fluxer_guild_id:
            self.synced = False
            self.states.clear()
        elif event == "GUILD_MEMBER_REMOVE" and data.get("guild_id") == self.config.fluxer_guild_id:
            uid = data.get("user", {}).get("id", data.get("user_id"))
            self.states = {k: v for k, v in self.states.items() if v.get("user_id") != uid}
        elif data.get("guild_id") == self.config.fluxer_guild_id:
            if event == "VOICE_STATE_UPDATE":
                self._voice(data)
            elif event == "VOICE_SERVER_UPDATE" and self.pending and not self.pending.done():
                if data.get("channel_id") == self.channel_id:
                    self.pending.set_result(data)
            elif event in {"CHANNEL_CREATE", "CHANNEL_UPDATE"}:
                self.channels[data["id"]] = data
            elif event == "CHANNEL_DELETE":
                self.channels.pop(data["id"], None)
        if self.ready and not was_ready:
            log.info("Fluxer Gateway connected; community membership synchronized")
        self.on_change()

    def _voice(self, data: dict) -> None:
        key = data.get("connection_id") or data.get("user_id")
        if not key:
            return
        if data.get("channel_id"):
            self.states[key] = data
        else:
            self.states.pop(key, None)

    async def _heartbeat(self, interval: float, sequence: list) -> None:
        while True:
            await asyncio.sleep(interval)
            if not self._acknowledged:
                await self.socket.close()
                return
            self._acknowledged = False
            await self.send(1, sequence[0])

    async def presence(self) -> None:
        previous = object()
        while True:
            if self.online:
                title = self.now_playing() if self.ready else None
                text = ("Watching " + title.strip())[:128] if title and title.strip() else None
                if text != previous:
                    try:
                        await self.send(3, {"status": "online", "afk": False,
                                            "custom_status": {"text": text} if text else None})
                        previous = text
                    except Exception:
                        log.warning("Could not update now-playing status")
            await asyncio.sleep(2)

    async def run(self) -> None:
        delay = 1
        while not self._closing:
            heartbeat = presence = None
            try:
                await self.discover()
                p = urlsplit(self.gateway)
                query = dict(parse_qsl(p.query)) | {"v": "1", "encoding": "json"}
                uri = urlunsplit((p.scheme, p.netloc, p.path, urlencode(query), ""))
                async with websockets.connect(uri, max_size=8 * 1024 * 1024, open_timeout=15) as socket:
                    self.socket = socket
                    presence = asyncio.create_task(self.presence())
                    sequence = [None]
                    self._acknowledged = True
                    async for raw in socket:
                        payload = json.loads(raw)
                        op, data = payload["op"], payload.get("d")
                        if op == 10:
                            heartbeat = asyncio.create_task(self._heartbeat(data["heartbeat_interval"] / 1000, sequence))
                            await self.send(2, {"token": self.config.fluxer_bot_token.get_secret_value(),
                                                "initial_guild_id": self.config.fluxer_guild_id,
                                                "properties": {"os": "linux", "browser": "jellyfin-cinema",
                                                               "device": "jellyfin-cinema", "e2ee_capable": True}})
                        elif op == 11:
                            self._acknowledged = True
                        elif op == 1:
                            await self.send(1, sequence[0])
                        elif op in {7, 9}:
                            break
                        elif op == 0:
                            sequence[0] = payload.get("s")
                            self.dispatch(payload["t"], data)
                            if self.ready:
                                delay = 1
            except asyncio.CancelledError:
                raise
            except Exception as e:
                log.warning("Gateway unavailable: %s", e.message if isinstance(e, Problem) else type(e).__name__)
            finally:
                if presence:
                    presence.cancel()
                    with contextlib.suppress(asyncio.CancelledError, Exception):
                        await presence
                if heartbeat:
                    heartbeat.cancel()
                    with contextlib.suppress(asyncio.CancelledError, Exception):
                        await heartbeat
                if self.online:
                    log.info("Fluxer Gateway disconnected; refreshing membership before controls resume")
                self.socket = None
                self.online = self.synced = False
                self.states.clear()
                if self.pending and not self.pending.done():
                    self.pending.set_exception(Problem(503, "Gateway disconnected during voice join."))
                self.on_change()
            await asyncio.sleep(delay)
            delay = min(30, delay * 2)

    async def close(self) -> None:
        self._closing = True
        await self.leave()
        if self.socket:
            if self.online:
                with contextlib.suppress(Exception):
                    await self.send(3, {"status": "online", "afk": False, "custom_status": None})
            await self.socket.close()
