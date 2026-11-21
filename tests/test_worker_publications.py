import asyncio
from types import SimpleNamespace

import pytest
from livekit import rtc

from cinema.worker import Worker


@pytest.fixture
async def publication_worker(monkeypatch):
    published, unpublished, syncs = [], [], []

    class Source:
        def __init__(self, *args, **kwargs):
            self.closed = False
            self.frames = []

        async def aclose(self):
            self.closed = True

        def capture_frame(self, frame):
            self.frames.append(frame)

    class Sync:
        def __init__(self, **kwargs):
            self.cleared = self.closed = False
            syncs.append(self)

        async def clear_queue(self):
            self.cleared = True

        async def aclose(self):
            self.closed = True

    async def publish(track, options):
        track.options = options
        published.append(track)

    async def unpublish(sid):
        unpublished.append(sid)

    def track(name, source):
        return SimpleNamespace(sid=f"{name}-{len(published)}", source=source)

    monkeypatch.setattr("cinema.worker.rtc.VideoSource", Source)
    monkeypatch.setattr("cinema.worker.rtc.AudioSource", Source)
    monkeypatch.setattr("cinema.worker.rtc.AVSynchronizer", Sync)
    monkeypatch.setattr("cinema.worker.rtc.LocalVideoTrack.create_video_track", track)
    monkeypatch.setattr("cinema.worker.rtc.LocalAudioTrack.create_audio_track", track)
    worker = Worker()
    worker.room = SimpleNamespace(local_participant=SimpleNamespace(
        publish_track=publish, unpublish_track=unpublish))
    return worker, published, unpublished, syncs


async def test_repeated_seek_reuses_publications_and_flushes_clocks(publication_worker):
    worker, published, unpublished, syncs = publication_worker
    decoder = SimpleNamespace(width=1920, height=1080, fps=24)
    await worker.tracks(decoder)
    sources = worker.video_source, worker.audio_source
    frame = object()
    worker.last_frame = frame
    for _ in range(3):
        previous = worker.sync
        await worker.cancel()
        assert previous.cleared and previous.closed
        await worker.tracks(decoder)
        assert worker.sync is not previous
        assert (worker.video_source, worker.audio_source) == sources
        assert worker.last_frame is frame
    assert len(published) == 2 and not unpublished
    assert not any(source.closed for source in sources)
    await worker.cancel()


@pytest.mark.parametrize("encoder,backend", [
    ("auto", rtc.VideoEncoderBackend.ENCODER_BACKEND_AUTO),
    ("software", rtc.VideoEncoderBackend.ENCODER_BACKEND_SOFTWARE),
    ("hardware", rtc.VideoEncoderBackend.ENCODER_BACKEND_HARDWARE),
    ("vaapi", rtc.VideoEncoderBackend.ENCODER_BACKEND_VAAPI),
    ("nvenc", rtc.VideoEncoderBackend.ENCODER_BACKEND_NVENC),
])
async def test_publication_requests_configured_encoder(publication_worker, encoder, backend):
    worker, published, _, _ = publication_worker
    worker.video_encoder = encoder
    await worker.tracks(SimpleNamespace(width=1920, height=1080, fps=24))
    options = published[0].options
    assert options.video_encoder == backend
    assert options.video_codec == rtc.VideoCodec.H264 and options.simulcast is False
    await worker.cancel()


@pytest.mark.parametrize("implementation,efficient", [("NvEncoder", True), ("VAAPI", True), ("OpenH264", False)])
async def test_stats_report_actual_encoder_including_software_fallback(publication_worker, implementation, efficient):
    worker, _, _, _ = publication_worker
    report = rtc.stats.RtcStats()
    report.outbound_rtp.stream.kind = "video"
    report.outbound_rtp.outbound.encoder_implementation = implementation
    report.outbound_rtp.outbound.power_efficient_encoder = efficient
    async def stats():
        return [report]
    worker.video_encoder = "nvenc"
    worker.video_track = SimpleNamespace(get_stats=stats)
    await worker.update_encoder_stats()
    assert worker.encoder_stats == {"encoder_implementation": implementation, "encoder_power_efficient": efficient}


async def test_missing_or_failed_stats_do_not_claim_hardware_or_break_playback(publication_worker):
    worker, _, _, _ = publication_worker
    async def stats():
        report = rtc.stats.RtcStats()
        report.outbound_rtp.stream.kind = "video"
        return [report]
    worker.video_track = SimpleNamespace(get_stats=stats)
    await worker.update_encoder_stats()
    assert worker.encoder_stats == {}
    async def failed():
        raise RuntimeError("stats unavailable")
    worker.video_track.get_stats = failed
    worker.encoder_stats = {"encoder_implementation": "stale"}
    await worker.update_encoder_stats()
    assert worker.encoder_stats == {}


async def test_stop_while_fetching_stats_does_not_kill_metrics_task(publication_worker, monkeypatch):
    worker, _, _, _ = publication_worker
    worker.sync = object()
    events = []
    monkeypatch.setattr("cinema.worker.emit", lambda *args, **kwargs: events.append(args))
    async def stop_during_stats():
        worker.sync = None
    monkeypatch.setattr(worker, "update_encoder_stats", stop_during_stats)
    calls = 0
    async def sleep(_):
        nonlocal calls
        calls += 1
        if calls > 1:
            raise asyncio.CancelledError
    monkeypatch.setattr("cinema.worker.asyncio.sleep", sleep)
    with pytest.raises(asyncio.CancelledError):
        await worker.metrics()
    assert not events


async def test_format_change_replaces_video_but_keeps_audio(publication_worker):
    worker, published, unpublished, _ = publication_worker
    await worker.tracks(SimpleNamespace(width=1920, height=1080, fps=24))
    video, audio = worker.video_source, worker.audio_source
    video_sid = worker.video_track.sid
    worker.last_frame = object()
    await worker.cancel()
    await worker.tracks(SimpleNamespace(width=1280, height=720, fps=30))
    assert video.closed and not audio.closed
    assert worker.audio_source is audio
    assert unpublished == [video_sid]
    assert len(published) == 3
    assert worker.last_frame is None
    await worker.cancel()


async def test_buffering_holds_picture_without_reporting_stale_progress(publication_worker, monkeypatch):
    worker, _, _, _ = publication_worker
    await worker.tracks(SimpleNamespace(width=1920, height=1080, fps=24))
    worker.last_frame = object()
    worker.buffering = True
    events = []
    monkeypatch.setattr("cinema.worker.emit", lambda *args, **kwargs: events.append(args))
    calls = 0

    async def sleep(_):
        nonlocal calls
        calls += 1
        if calls > 1:
            raise asyncio.CancelledError

    monkeypatch.setattr("cinema.worker.asyncio.sleep", sleep)
    with pytest.raises(asyncio.CancelledError):
        await worker.metrics()
    assert worker.video_source.frames == [worker.last_frame]
    assert not events
    await worker.cancel()
