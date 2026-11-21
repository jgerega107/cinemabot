"""Isolated media process; stdin commands and stdout events are newline-delimited JSON.

Never log source URLs, LiveKit grants, native decoder exceptions, or inherited secrets.
"""
import asyncio
import contextlib
import json
import sys
import time

from livekit import rtc

from cinema.media import Decoder, scale_audio


def emit(event: str, **data):
    print(json.dumps({"event": event, **data}), flush=True)


def pull(iterator):
    return next(iterator, None)


class Worker:
    def __init__(self):
        self.room = rtc.Room()
        self.task = None
        self.sync = self.audio_source = self.video_source = None
        self.video_track = self.audio_track = None
        self.volume = 70
        self.generation = 0
        self.paused = False
        self.pause_start = 0.0
        self.pause_total = 0.0
        self.started = 0.0
        self.offset = 0.0
        self.position = 0.0
        self.last_frame = None
        self.buffering = False
        self.video_format = None
        self.disconnected = False
        self.intentional_close = False
        self.bitrate = 6_000_000
        self.video_encoder = "auto"
        self.encoder_stats = {}

        @self.room.on("disconnected")
        def disconnected(*_):
            self.disconnected = True
            if not self.intentional_close:
                self.pause()
                emit("disconnected", generation=self.generation)

        @self.room.on("reconnecting")
        def reconnecting(*_):
            self.pause()
            emit("disconnected", generation=self.generation)

    async def connect(self, command):
        grant = command["grant"]
        self.bitrate = command.get("bitrate", self.bitrate)
        self.video_encoder = command.get("video_encoder", "auto")
        # Validate before connecting; this is a preference, not proof of GPU use.
        self.encoder_backend()
        encryption = None
        if grant.get("e2ee_key"):
            encryption = rtc.E2EEOptions(key_provider_options=rtc.KeyProviderOptions(
                shared_key=grant["e2ee_key"].encode("utf-8")))
        await self.room.connect(grant["endpoint"], grant["token"],
                                rtc.RoomOptions(auto_subscribe=False, dynacast=False,
                                                encryption=encryption, connect_timeout=15))
        self.disconnected = False
        emit("connected")

    async def cancel(self):
        if self.task:
            self.task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await self.task
            self.task = None
        if self.sync:
            await self.sync.clear_queue()
            await self.sync.aclose()
            self.sync = None

    def pause(self):
        if not self.paused:
            self.paused = True
            self.pause_start = time.monotonic()

    def resume(self):
        if self.paused:
            self.pause_total += time.monotonic() - self.pause_start
            self.paused = False
            if self.sync:
                self.sync.reset()

    async def wait_until(self, target: float):
        while True:
            if self.paused:
                await asyncio.sleep(0.02)
                continue
            delay = self.started + self.pause_total + target - time.monotonic()
            if delay <= 0:
                return
            await asyncio.sleep(min(delay, 0.02))

    async def tracks(self, decoder: Decoder):
        video_format = (decoder.width, decoder.height, decoder.fps)
        # Seeking and track changes normally keep the same output format.
        # Retain publications so receivers don't unsubscribe/reacquire the stream.
        if self.video_track is None or self.video_format != video_format:
            if self.video_track:
                await self.room.local_participant.unpublish_track(self.video_track.sid)
            if self.video_source:
                await self.video_source.aclose()
            self.last_frame = None
            self.encoder_stats = {}
            self.video_source = rtc.VideoSource(decoder.width, decoder.height)
            self.video_track = rtc.LocalVideoTrack.create_video_track("cinema-video", self.video_source)
            await self.room.local_participant.publish_track(self.video_track, rtc.TrackPublishOptions(
                source=rtc.TrackSource.SOURCE_SCREENSHARE, simulcast=False, video_codec=rtc.VideoCodec.H264,
                video_encoder=self.encoder_backend(),
                video_encoding=rtc.VideoEncoding(max_framerate=decoder.fps, max_bitrate=self.bitrate)))
            self.video_format = video_format
        # All decoded audio is normalized to 48 kHz stereo, even across titles.
        if self.audio_track is None:
            self.audio_source = rtc.AudioSource(48000, 2, queue_size_ms=50)
            self.audio_track = rtc.LocalAudioTrack.create_audio_track("cinema-audio", self.audio_source)
            await self.room.local_participant.publish_track(self.audio_track, rtc.TrackPublishOptions(
                source=rtc.TrackSource.SOURCE_SCREENSHARE_AUDIO, dtx=False, red=True,
                audio_encoding={"max_bitrate": 128_000}))
        # cancel() flushed old frames and closed the previous synchronizer.
        # A fresh clock supports backward seeks without reusing old timestamps.
        self.sync = rtc.AVSynchronizer(audio_source=self.audio_source, video_source=self.video_source,
                                       video_fps=decoder.fps, video_queue_size_ms=50)

    async def play(self, spec: dict, generation: int):
        decoder = None
        pending_decode = None
        self.buffering = True
        try:
            # Shield native work from cancellation, then join it before closing its container.
            pending_decode = asyncio.create_task(asyncio.to_thread(Decoder, spec))
            decoder = await asyncio.shield(pending_decode)
            pending_decode = None
            await self.tracks(decoder)
            self.offset = float(spec.get("offset", 0))
            self.position = self.offset
            self.started = time.monotonic()
            self.pause_total = 0
            if self.paused:
                self.pause_start = self.started
            emit("playing", generation=generation, width=decoder.width, height=decoder.height, fps=decoder.fps)
            iterator = decoder.frames()
            while True:
                pending_decode = asyncio.create_task(asyncio.to_thread(pull, iterator))
                decoded = await asyncio.shield(pending_decode)
                pending_decode = None
                if decoded is None:
                    break
                await self.wait_until(decoded.time)
                if decoded.kind == "video":
                    frame = rtc.VideoFrame(decoded.width, decoded.height, rtc.VideoBufferType.I420, decoded.data)
                    await self.sync.push(frame, self.offset + decoded.time)
                    self.last_frame = frame
                    self.buffering = False
                else:
                    frame = rtc.AudioFrame(scale_audio(decoded.data, self.volume), 48000, 2, decoded.samples)
                    await self.sync.push(frame, self.offset + decoded.time + decoded.samples / 48000)
                self.position = max(self.position, self.offset + decoded.time)
            await self.sync.wait_for_playout()
            emit("ended", generation=generation, position=self.position)
        except asyncio.CancelledError:
            raise
        except Exception:
            self.buffering = False
            emit("error", generation=generation, message="Media decoding or publishing failed.")
        finally:
            if pending_decode:
                with contextlib.suppress(Exception):
                    result = await pending_decode
                    if isinstance(result, Decoder):
                        decoder = result
            if decoder:
                await asyncio.to_thread(decoder.close)

    def encoder_backend(self):
        return {
            "auto": rtc.VideoEncoderBackend.ENCODER_BACKEND_AUTO,
            "software": rtc.VideoEncoderBackend.ENCODER_BACKEND_SOFTWARE,
            "hardware": rtc.VideoEncoderBackend.ENCODER_BACKEND_HARDWARE,
            "vaapi": rtc.VideoEncoderBackend.ENCODER_BACKEND_VAAPI,
            "nvenc": rtc.VideoEncoderBackend.ENCODER_BACKEND_NVENC,
        }[self.video_encoder]

    async def update_encoder_stats(self):
        track = self.video_track
        if track is None:
            return
        result = {}
        try:
            async with asyncio.timeout(1):
                reports = await track.get_stats()
            for report in reports:
                if not report.HasField("outbound_rtp") or report.outbound_rtp.stream.kind != "video":
                    continue
                outbound = report.outbound_rtp.outbound
                if outbound.HasField("encoder_implementation") and outbound.encoder_implementation:
                    # Forward only a bounded, printable SDK diagnostic, never the full stats report.
                    result["encoder_implementation"] = "".join(
                        c for c in outbound.encoder_implementation[:128] if c.isascii() and c.isprintable())
                if outbound.HasField("power_efficient_encoder"):
                    result["encoder_power_efficient"] = outbound.power_efficient_encoder
        except Exception:
            pass  # Unsupported/temporarily unavailable stats must not interrupt playback.
        if track is self.video_track:
            self.encoder_stats = result

    async def metrics(self):
        next_stats = 0.0
        while True:
            await asyncio.sleep(1)
            if (self.paused or self.buffering) and self.last_frame and self.video_source:
                # Keep a keyframe-capable publication for viewers who arrive during a pause.
                self.video_source.capture_frame(self.last_frame)
            if self.sync and not self.buffering:
                if time.monotonic() >= next_stats:
                    await self.update_encoder_stats()
                    next_stats = time.monotonic() + 5
                # A stop/seek may have cleared the synchronizer while stats were in flight.
                if self.sync is None or self.buffering:
                    continue
                emit("progress", generation=self.generation, position=self.position, paused=self.paused,
                     encoder_requested=self.video_encoder, **self.encoder_stats,
                     fps=round(self.sync.actual_fps, 2),
                     av_drift_ms=round((self.sync.last_video_time - self.sync.last_audio_time) * 1000, 1))

    async def run(self):
        metrics = asyncio.create_task(self.metrics())
        try:
            while line := await asyncio.to_thread(sys.stdin.readline):
                command = json.loads(line)
                action = command["command"]
                if action == "connect":
                    await self.connect(command)
                elif action == "play":
                    self.buffering = True
                    await self.cancel()
                    self.generation = command["generation"]
                    self.paused = command.get("paused", False)
                    self.task = asyncio.create_task(self.play(command["source"], self.generation))
                elif action == "pause":
                    self.pause()
                elif action == "resume":
                    self.resume()
                elif action == "volume":
                    self.volume = command["value"]
                elif action == "stop":
                    # Parent uses stop to prepare a replacement source; a full
                    # user stop closes the worker and disconnects the room.
                    self.buffering = True
                    await self.cancel()
                elif action == "close":
                    break
        finally:
            self.intentional_close = True
            metrics.cancel()
            await self.cancel()
            await self.room.disconnect()
            for source in (self.video_source, self.audio_source):
                if source:
                    await source.aclose()


async def run_worker():
    # Room binds its callbacks and futures to the current loop at construction.
    await Worker().run()


if __name__ == "__main__":
    raise SystemExit("Start the confined worker with python -m cinema.worker_entry")
