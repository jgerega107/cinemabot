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
        self.audio = next((s for s in self.container.streams.audio if s.index == spec.get("audio_index")), None)
        if self.audio is None and self.container.streams.audio:
            self.audio = self.container.streams.audio[0]
        self.width, self.height = dimensions(self.video.width, self.video.height,
                                            float(self.video.sample_aspect_ratio or 1))
        rate = float(self.video.average_rate or 24)
        self.fps = min(30.0, max(1.0, rate if math.isfinite(rate) else 24))
        if self.seek:
            self.container.seek(int((self.seek + self.origin) * av.time_base))

    def frames(self):
        resampler = av.AudioResampler(format="s16", layout="stereo", rate=48000)
        next_video = 0.0
        last_video = None
        audio_fallback = 0.0
        streams = [self.video] + ([self.audio] if self.audio else [])
        for packet in self.container.demux(streams):
            for decoded in packet.decode():
                if isinstance(decoded, av.VideoFrame):
                    t = (float(decoded.time) if decoded.time is not None else self.origin + self.seek + next_video)
                    t -= self.origin + self.seek
                    if t < -1 / self.fps:
                        continue
                    # A constant output grid handles VFR and drops >30fps input without speeding it up.
                    if t + 1e-6 < next_video:
                        continue
                    if t - next_video > 10:
                        raise ValueError("Discontinuous media timestamps")
                    raw = decoded.reformat(width=self.width, height=self.height, format="yuv420p")
                    data = raw.to_ndarray().tobytes()
                    while next_video <= t + 1e-6:
                        yield Frame("video", next_video, last_video or data, self.width, self.height)
                        if not self.audio:
                            # Silent movies still have a shared audio/video clock.
                            count = round(48000 / self.fps)
                            yield Frame("audio", next_video, bytes(count * 4), samples=count)
                        next_video += 1 / self.fps
                    last_video = data
                else:
                    for frame in resampler.resample(decoded):
                        t = float(frame.time) - self.origin - self.seek if frame.time is not None else audio_fallback
                        pcm = frame.to_ndarray().reshape(-1, 2)
                        if t < 0:
                            trim = min(len(pcm), round(-t * 48000))
                            pcm = pcm[trim:]
                            t += trim / 48000
                        for start in range(0, len(pcm), 960):
                            block = pcm[start:start + 960]
                            if len(block):
                                yield Frame("audio", max(0, t + start / 48000), block.tobytes(), samples=len(block))
                        audio_fallback = max(0, t) + len(pcm) / 48000
        for frame in resampler.resample(None):
            data = frame.to_ndarray().reshape(-1, 2)
            yield Frame("audio", audio_fallback, data.tobytes(), samples=len(data))
            audio_fallback += len(data) / 48000
        if last_video:
            yield Frame("video", next_video, last_video, self.width, self.height)

    def close(self):
        self.container.close()


def scale_audio(data: bytes, volume: int) -> bytes:
    if volume == 100:
        return data
    samples = np.frombuffer(data, dtype=np.int16).astype(np.float32)
    return np.clip(samples * volume / 100, -32768, 32767).astype(np.int16).tobytes()
