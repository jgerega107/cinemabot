"""Present decoded audio and video in FFmpeg's interleaved timestamp order."""

import os
import threading
from fractions import Fraction

import av

from cinema.media import Frame


class Presentation:
    """NUT carries timestamped I420/PCM locally. The pipe bounds producer read-ahead."""

    def __init__(self, decoder):
        self.decoder = decoder
        read, write = os.pipe()
        self.reader = os.fdopen(read, "rb", buffering=0)
        self.writer = os.fdopen(write, "wb", buffering=0)
        self.error = None
        self.stopping = threading.Event()
        self.thread = threading.Thread(target=self._produce, name="ffmpeg-interleave", daemon=True)
        self.thread.start()

    def _produce(self):
        try:
            with (
                self.writer,
                av.open(
                    self.writer,
                    "w",
                    format="nut",
                    options={"max_interleave_delta": "1500000", "flush_packets": "1"},
                ) as mux,
            ):
                video = mux.add_stream("rawvideo", rate=Fraction(self.decoder.fps).limit_denominator(1001000))
                video.width, video.height, video.pix_fmt = self.decoder.width, self.decoder.height, "yuv420p"
                video.time_base = Fraction(1, 90000)
                audio = mux.add_stream("pcm_s16le", rate=48000)
                audio.layout = "stereo"
                audio.time_base = Fraction(1, 48000)
                mux.start_encoding()
                for frame in self.decoder.frames():
                    if self.stopping.is_set():
                        break
                    stream = video if frame.kind == "video" else audio
                    packet = av.Packet(frame.data)
                    packet.stream = stream
                    packet.time_base = stream.time_base
                    packet.pts = packet.dts = round(frame.time / stream.time_base)
                    packet.is_keyframe = True
                    mux.mux(packet)
        except Exception as exc:
            self.error = exc

    def frames(self):
        # Both formats and dimensions are signaled in the NUT header.
        with av.open(
            self.reader, "r", format="nut", options={"probesize": "32", "analyzeduration": "1"}
        ) as source:
            for packet in source.demux():
                if packet.pts is None or not packet.size:
                    continue
                t = float(packet.pts * packet.time_base)
                if packet.stream.type == "video":
                    yield Frame("video", t, bytes(packet), self.decoder.width, self.decoder.height)
                else:
                    yield Frame("audio", t, bytes(packet), samples=packet.size // 4)
        if self.error:
            raise self.error

    def close(self):
        self.stopping.set()
        self.reader.close()
        self.thread.join()
