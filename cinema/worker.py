"""Publish FFmpeg's H.264 and Opus samples on the bot's screen-share tracks."""

import asyncio
import contextlib
import json
import sys
import time
from collections import deque

from cinema.encoded import OpusEncoder, Publisher, VideoEncoder
from cinema.interleave import Presentation
from cinema.media import Decoder


def emit(event, **fields):
    print(json.dumps({"event": event, **fields}), flush=True)


class Worker:
    def __init__(self):
        self.publisher = Publisher()
        self.task = None
        self.paused = False
        self.buffering = True
        self.generation = 0
        self.volume = 70
        self.encoder = None
        self.format = None
        self.last_picture = None
        self.last_idr = None
        self.position = 0.0
        self.offset = 0.0
        self.fps_samples = deque(maxlen=30)
        self.video_time = self.audio_time = None
        self.pause_start = self.origin = 0.0
        self.epoch_ns = time.time_ns() - time.monotonic_ns()
        self.last_send = {}
        self.backend = "auto"
        self.bitrate = 6000000
        self.disconnected = False

    def pause(self):
        if not self.paused:
            self.pause_start = time.monotonic()
            self.paused = True

    def resume(self):
        if self.paused:
            self.origin += time.monotonic() - self.pause_start
            self.paused = False
            self.fps_samples.clear()

    async def wait_until(self, pts):
        while True:
            remaining = self.origin + pts - time.monotonic()
            if not self.paused and remaining <= 0:
                return
            await asyncio.sleep(0.02 if self.paused else min(remaining, 0.02))

    def send(self, video, payload, duration, when):
        # A new generation or pause can reset media PTS. RTP tracks keep their clock.
        stamp = max(self.last_send.get(video, 0) + 1, self.epoch_ns + round(when * 1e9))
        self.publisher.write(video, payload, duration, stamp)
        self.last_send[video] = stamp

    async def cancel(self):
        if self.task:
            self.task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self.task
            self.task = None

    async def tracks(self, decoder):
        fmt = decoder.width, decoder.height, decoder.fps
        if self.format != fmt:
            if self.encoder:
                self.encoder.close()
                self.encoder = None
            self.last_idr = self.last_picture = None
            self.format = None
            self.encoder = VideoEncoder(self.backend, *fmt, self.bitrate)
            # A seek can cancel playback during room signaling. Finish the native
            # operation before another generation can replace the same tracks.
            publishing = asyncio.create_task(
                asyncio.to_thread(self.publisher.tracks, decoder.width, decoder.height)
            )
            try:
                await asyncio.shield(publishing)
            except asyncio.CancelledError:
                await publishing
                raise
            self.format = fmt

    def audio_packets(self, packets):
        for packet in packets:
            pts = max(0.0, float(packet.pts * packet.time_base))
            self.send(False, bytes(packet), 0.020, self.origin + pts)
            self.audio_time = pts + 0.020

    async def play(self, spec, generation):
        decoder = presentation = iterator = pending = None
        try:
            pending = asyncio.create_task(asyncio.to_thread(Decoder, spec))
            decoder = await asyncio.shield(pending)
            pending = None
            await self.tracks(decoder)
            opus = OpusEncoder()
            presentation = Presentation(decoder)
            iterator = presentation.frames()
            self.offset = self.position = float(spec.get("offset", 0))
            self.video_time = self.audio_time = None
            self.fps_samples.clear()
            first_sample = True
            first_video = True
            while True:
                pending = asyncio.create_task(asyncio.to_thread(next, iterator, None))
                frame = await asyncio.shield(pending)
                pending = None
                if frame is None:
                    break
                if first_sample:
                    self.origin = self.pause_start = time.monotonic()
                    emit(
                        "playing",
                        generation=generation,
                        width=decoder.width,
                        height=decoder.height,
                        fps=decoder.fps,
                    )
                    first_sample = False
                await self.wait_until(frame.time)
                if frame.kind == "video":
                    payload = self.encoder.encode(frame.data, first_video or self.publisher.needs_keyframe())
                    self.send(True, payload, 1 / decoder.fps, self.origin + frame.time)
                    self.last_picture, self.last_idr = frame.data, None
                    first_video = False
                    self.buffering = False
                    self.fps_samples.append(time.monotonic())
                    self.video_time = frame.time
                else:
                    self.audio_packets(opus.encode(frame, self.volume))
                self.position = max(self.position, self.offset + frame.time)
            self.audio_packets(opus.finish())
            if first_video:
                raise RuntimeError("Empty video stream")
            emit("ended", generation=generation, position=self.position)
        except asyncio.CancelledError:
            raise
        except Exception:
            emit("error", generation=generation, message="FFmpeg decoding, encoding or publishing failed.")
        finally:
            self.buffering = True
            if pending:
                with contextlib.suppress(Exception):
                    result = await pending
                    if isinstance(result, Decoder):
                        decoder = result
            if iterator:
                iterator.close()
            if presentation:
                await asyncio.to_thread(presentation.close)
            if decoder:
                await asyncio.to_thread(decoder.close)

    async def metrics(self):
        while True:
            await asyncio.sleep(1)
            if not self.disconnected and self.publisher.interrupted():
                self.disconnected = True
                self.pause()
                emit("disconnected", generation=self.generation)
            if (self.paused or self.buffering) and self.last_picture and self.encoder:
                # Repeat an independently decodable picture for late subscribers.
                try:
                    if self.last_idr is None:
                        self.last_idr = self.encoder.encode(self.last_picture, True)
                    self.send(True, self.last_idr, 1 / self.format[2], time.monotonic())
                except Exception:
                    emit("error", generation=self.generation, message="Could not publish the paused picture.")
                    return
            if self.buffering or self.video_time is None or self.audio_time is None:
                continue
            times = self.fps_samples
            fps = (len(times) - 1) / (times[-1] - times[0]) if len(times) > 1 else 0.0
            emit(
                "progress",
                generation=self.generation,
                position=self.position,
                paused=self.paused,
                encoder_requested=self.backend,
                encoder_implementation=self.encoder.name,
                encoder_power_efficient=self.encoder.name != "libx264",
                fps=round(fps, 2),
                av_drift_ms=round((self.video_time - self.audio_time) * 1000, 1),
            )

    async def run(self):
        metrics = None
        try:
            while line := await asyncio.to_thread(sys.stdin.readline):
                cmd = json.loads(line)
                match cmd["command"]:
                    case "connect":
                        self.bitrate = cmd["bitrate"]
                        self.backend = cmd.get("video_encoder", "auto")
                        await asyncio.to_thread(self.publisher.connect, cmd["grant"])
                        emit("connected")
                        metrics = asyncio.create_task(self.metrics())
                    case "play":
                        await self.cancel()
                        self.generation = cmd["generation"]
                        self.paused = cmd.get("paused", False)
                        self.buffering = True
                        self.task = asyncio.create_task(self.play(cmd["source"], self.generation))
                    case "stop":
                        await self.cancel()
                    case "pause":
                        self.pause()
                    case "resume":
                        self.resume()
                    case "volume":
                        self.volume = cmd["value"]
                    case "close":
                        break
        finally:
            if metrics:
                metrics.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await metrics
            await self.cancel()
            self.publisher.close()
            if self.encoder:
                self.encoder.close()


async def run_worker():
    await Worker().run()
