import asyncio
from types import SimpleNamespace

import pytest

from cinema.encoded import OpusEncoder, VideoEncoder
from cinema.interleave import Presentation
from cinema.media import Frame
from cinema.worker import Worker


def test_ffmpeg_interleaves_skewed_audio_before_publication():
    def frames():
        # A full half second of audio arrives before the first picture in each group.
        for group in range(4):
            for i in range(25):
                yield Frame("audio", group * .5 + i * .02, bytes(3840), samples=960)
            for i in range(12):
                yield Frame("video", group * .5 + i / 24, bytes(64 * 48 * 3 // 2), 64, 48)
    presentation = Presentation(SimpleNamespace(width=64, height=48, fps=24, frames=frames))
    try:
        ordered = list(presentation.frames())
    finally:
        presentation.close()
    timestamps = [frame.time for frame in ordered]
    assert timestamps == sorted(timestamps)
    pictures = [f for f in ordered if f.kind == "video"]
    assert len(pictures) == 48
    assert max(b.time - a.time for a, b in zip(pictures, pictures[1:], strict=False)) == pytest.approx(1 / 24, abs=.00002)
    assert sum(f.samples for f in ordered) == 96000


def test_opus_accepts_variable_pcm_blocks_and_flushes():
    opus = OpusEncoder()
    sizes = [960, 64, 1920, 128, 960]
    pts = 0
    packets = []
    for n in sizes:
        packets.extend(opus.encode(Frame("audio", pts / 48000, bytes(n*4), samples=n), 70))
        pts += n
    packets += opus.finish()
    assert packets and all(bytes(p) for p in packets)
    assert [p.pts for p in packets] == sorted(p.pts for p in packets)


async def test_reuses_tracks_and_sends_decodable_paused_picture(monkeypatch):
    class Sink:
        def __init__(self):
            self.formats, self.samples = [], []
        def tracks(self, w, h):
            self.formats.append((w, h))
        def write(self, video, payload, duration, timestamp):
            self.samples.append(payload)
        def interrupted(self):
            return False
    monkeypatch.setattr("cinema.worker.Publisher", Sink)
    worker = Worker()
    worker.backend = "software"
    fmt = SimpleNamespace(width=64, height=48, fps=24)
    await worker.tracks(fmt)
    await worker.tracks(fmt)
    assert worker.publisher.formats == [(64, 48)]
    worker.last_picture = bytes(64*48*3//2)
    worker.pause()
    calls = 0
    async def tick(_):
        nonlocal calls
        calls += 1
        if calls == 3:
            raise asyncio.CancelledError
    monkeypatch.setattr("cinema.worker.asyncio.sleep", tick)
    with pytest.raises(asyncio.CancelledError):
        await worker.metrics()
    assert len(worker.publisher.samples) == 2
    import av
    for payload in worker.publisher.samples:
        decoder = av.CodecContext.create("h264", "r")
        assert len(decoder.decode(av.Packet(payload))) == 1
    await worker.tracks(SimpleNamespace(width=96, height=48, fps=24))
    assert worker.last_picture is None and worker.publisher.formats[-1] == (96, 48)
    worker.encoder.close()


def test_software_encoder_reports_opened_backend():
    encoder = VideoEncoder("software", 64, 48, 24, 500000)
    try:
        assert encoder.name == "libx264"
        assert encoder.encode(bytes(64*48*3//2), True)
    finally:
        encoder.close()
