"""Timestamp-preserving decoding. This module has no network/Fluxer side effects."""
import math
from dataclasses import dataclass

import av
import numpy as np


@dataclass
class Frame:
    kind: str
    time: float
    data: bytes
    width: int = 0
    height: int = 0
    samples: int = 0


def dimensions(width: int, height: int, sar: float = 1.0) -> tuple[int, int]:
    display_width = width * sar
    ratio = min(1.0, 1920 / display_width, 1080 / height)
    return max(2, int(display_width * ratio) // 2 * 2), max(2, int(height * ratio) // 2 * 2)


class Decoder:
    def __init__(self, spec: dict):
        # URLs/headers arrive only from the trusted Jellyfin resolver or the local CLI probe.
        options = {"rw_timeout": "15000000", "protocol_whitelist": "file,http,https,tcp,tls,crypto",
                   "headers": "".join(f"{k}: {v}\r\n" for k, v in spec.get("headers", {}).items())}
        self.container = av.open(spec["url"], options=options, timeout=(15, 15))
        self.seek = float(spec.get("seek", 0))
        self.origin = float(self.container.start_time or 0) / av.time_base
        videos = self.container.streams.video
        if not videos:
            self.container.close()
            raise ValueError("No video stream")
        self.video = videos[0]
        ordinal = spec.get("audio_ordinal")
        if ordinal is not None:
            tracks = self.container.streams.audio
            self.audio = tracks[ordinal] if isinstance(ordinal, int) and 0 <= ordinal < len(tracks) else None
        else:
            self.audio = next((s for s in self.container.streams.audio if s.index == spec.get("audio_index")), None)
        if self.audio is None and (ordinal is not None or spec.get("audio_index") is not None):
            self.container.close()
            raise ValueError("Selected audio stream is missing from the media")
        if self.audio is None and self.container.streams.audio:
            self.audio = self.container.streams.audio[0]
        self.width, self.height = dimensions(self.video.width, self.video.height,
                                            float(self.video.sample_aspect_ratio or 1))
        rate = float(self.video.average_rate or 24)
        self.fps = min(30.0, max(1.0, rate if math.isfinite(rate) else 24))
        if self.seek:
            self.container.seek(int((self.seek + self.origin) * av.time_base))

    def frames(self):
        resampler = av.AudioResampler(format="s16", layout="stereo", rate=48000, frame_size=960)
        next_video = 0
        last_video = None
        next_audio_sample = None

        def audio_blocks(decoded):
            nonlocal next_audio_sample
            for frame in resampler.resample(decoded):
                start = next_audio_sample if next_audio_sample is not None else 0
                if frame.time is not None:
                    pts = round((frame.time - self.origin - self.seek) * 48000)
                    # Allow 2 ms of container timestamp rounding (96 samples at 48 kHz).
                    # Anchor at the first PTS and preserve larger gaps in the source.
                    if next_audio_sample is None or abs(pts - start) > 96:
                        start = pts
                next_audio_sample = start + frame.samples
                trim = max(0, -start)
                if trim < frame.samples:
                    pcm = frame.to_ndarray().reshape(-1, 2)[trim:]
                    yield Frame("audio", max(0, start) / 48000, pcm.tobytes(), samples=len(pcm))

        streams = [self.video] + ([self.audio] if self.audio else [])
        for packet in self.container.demux(streams):
            for decoded in packet.decode():
                if isinstance(decoded, av.VideoFrame):
                    t = (float(decoded.time) if decoded.time is not None
                         else self.origin + self.seek + next_video / self.fps)
                    t -= self.origin + self.seek
                    if t < -1 / self.fps:
                        continue
                    # Container timestamps may be rounded (e.g. Matroska milliseconds).
                    # Map to the nearest output slot instead of treating a slightly
                    # early timestamp as a frame to drop.
                    slot = math.floor(t * self.fps + 0.5)
                    if slot < next_video:
                        continue
                    if (slot - next_video) / self.fps > 10:
                        raise ValueError("Discontinuous media timestamps")
                    raw = decoded.reformat(width=self.width, height=self.height, format="yuv420p")
                    data = raw.to_ndarray().tobytes()
                    while next_video <= slot:
                        timestamp = next_video / self.fps
                        pixels = data if next_video == slot else (last_video or data)
                        yield Frame("video", timestamp, pixels, self.width, self.height)
                        if not self.audio:
                            # Silent movies still have a shared audio/video clock.
                            count = round(48000 / self.fps)
                            yield Frame("audio", timestamp, bytes(count * 4), samples=count)
                        next_video += 1
                    last_video = data
                else:
                    yield from audio_blocks(decoded)
        yield from audio_blocks(None)

    def close(self):
        self.container.close()


def scale_audio(data: bytes, volume: int) -> bytes:
    if volume == 100:
        return data
    samples = np.frombuffer(data, dtype=np.int16).astype(np.float32)
    return np.clip(samples * volume / 100, -32768, 32767).astype(np.int16).tobytes()
