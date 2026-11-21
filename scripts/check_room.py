"""Loopback integration check. Requires a local LiveKit dev server and FFmpeg CLI."""

import asyncio
import base64
import hashlib
import hmac
import json
import math
import sys
import time
from pathlib import Path

from livekit import rtc

from cinema.supervisor import Supervisor


def grant(identity, room):
    def segment(obj):
        return base64.urlsafe_b64encode(json.dumps(obj).encode()).rstrip(b"=")

    body = b".".join(
        [
            segment({"alg": "HS256", "typ": "JWT"}),
            segment(
                {
                    "iss": "devkey",
                    "sub": identity,
                    "exp": int(time.time()) + 120,
                    "video": {"roomJoin": True, "room": room, "canSubscribe": True, "canPublish": True},
                }
            ),
        ]
    )
    signature = hmac.digest(b"secret", body, hashlib.sha256)
    return (body + b"." + base64.urlsafe_b64encode(signature).rstrip(b"=")).decode()


async def check(clip, endpoint, encrypted):
    room_name = "cinema-loopback-" + str(time.time_ns())
    viewer = rtc.Room()
    streams, readers, video, audio = [], [], [], []
    received = asyncio.Event()
    key = "cinema-loopback-test-key" if encrypted else ""
    encryption = (
        rtc.E2EEOptions(key_provider_options=rtc.KeyProviderOptions(shared_key=key.encode())) if key else None
    )
    events = asyncio.Queue()
    publisher = Supervisor(events.put_nowait, local_file=clip)

    async def wait_for_progress(generation):
        async with asyncio.timeout(12):
            while True:
                event = await events.get()
                if event["event"] in {"fatal", "disconnected", "error"}:
                    raise RuntimeError(event.get("message", event["event"]))
                if event.get("generation") == generation and event["event"] == "progress":
                    return

    async def receive(track, stream):
        async for event in stream:
            if track.kind == rtc.TrackKind.KIND_VIDEO:
                video.append((time.monotonic(), event.frame.width, event.frame.height))
            else:
                if any(event.frame.data):
                    audio.append(event.frame.samples_per_channel)
            received.set()

    async def wait_for(condition):
        async with asyncio.timeout(12):
            while not condition():
                received.clear()
                await received.wait()

    @viewer.on("track_subscribed")
    def subscribed(track, publication, _participant):
        is_video = track.kind == rtc.TrackKind.KIND_VIDEO
        assert publication.source == (
            rtc.TrackSource.SOURCE_SCREENSHARE if is_video else rtc.TrackSource.SOURCE_SCREENSHARE_AUDIO
        )
        stream = rtc.VideoStream(track) if is_video else rtc.AudioStream(track)
        streams.append(stream)
        readers.append(asyncio.create_task(receive(track, stream)))

    try:
        await publisher.connect(
            {"endpoint": endpoint, "token": grant("bot", room_name), "e2ee_key": key}, 6_000_000, "software"
        )
        await publisher.send("play", source={"url": str(clip)}, generation=1)
        await wait_for_progress(1)
        await publisher.send("pause")
        await viewer.connect(endpoint, grant("viewer", room_name), rtc.RoomOptions(encryption=encryption))
        await wait_for(lambda: bool(video))
        await publisher.send("resume")
        await wait_for(lambda: len(video) >= 42 and sum(audio) >= 48000)
        before = len(video)
        await publisher.send("play", source={"url": str(clip), "seek": 1.0, "offset": 1.0}, generation=2)
        await wait_for_progress(2)
        await wait_for(lambda: len(video) >= before + 28)
        assert len(streams) == 2, "Seek replaced the viewer subscriptions"
        intervals = sorted(b[0] - a[0] for a, b in zip(video[-24:-1], video[-23:], strict=True))
        print(
            json.dumps(
                {
                    "encrypted": encrypted,
                    "video_frames": len(video),
                    "audio_samples": sum(audio),
                    "dimensions": video[-1][1:],
                    "p95_interval_ms": round(intervals[math.ceil(0.95 * len(intervals)) - 1] * 1000, 1),
                }
            ),
            flush=True,
        )
    finally:
        await publisher.close()
        for reader in readers:
            reader.cancel()
        await asyncio.gather(*readers, return_exceptions=True)
        for stream in streams:
            await stream.aclose()
        await viewer.disconnect()


if __name__ == "__main__":
    asyncio.run(check(Path(sys.argv[1]).resolve(), "ws://127.0.0.1:17880", "--encrypted" in sys.argv))
