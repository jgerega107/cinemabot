import asyncio
import math
import shutil
import subprocess
from fractions import Fraction

import av
import numpy as np
import pytest

from cinema.media import Decoder, dimensions, scale_audio
from cinema.worker import Worker


@pytest.fixture(scope="module")
def clip(tmp_path_factory):
    path = tmp_path_factory.mktemp("media") / "clip.mkv"
    with av.open(str(path), "w") as container:
        video = container.add_stream("mpeg4", rate=25)
        video.width, video.height, video.pix_fmt = 320, 180, "yuv420p"
        audio = container.add_stream("pcm_s16le", rate=48000)
        audio.layout = "stereo"
        for index in range(50):
            rgb = np.full((180, 320, 3), index * 5, dtype=np.uint8)
            frame = av.VideoFrame.from_ndarray(rgb, format="rgb24")
            frame.pts, frame.time_base = index, Fraction(1, 25)
            for packet in video.encode(frame):
                container.mux(packet)
            samples = (np.sin(np.arange(1920) * 2 * math.pi * 440 / 48000) * 5000).astype(np.int16)
            stereo = np.repeat(samples, 2).reshape(1, -1)
            aframe = av.AudioFrame.from_ndarray(stereo, format="s16", layout="stereo")
            aframe.sample_rate, aframe.pts, aframe.time_base = 48000, index * 1920, Fraction(1, 48000)
            for packet in audio.encode(aframe):
                container.mux(packet)
        for stream in (video, audio):
            for packet in stream.encode(None):
                container.mux(packet)
    return path


def test_real_decode_preserves_timeline_and_stereo(clip):
    decoder = Decoder({"url": str(clip)})
    try:
        frames = list(decoder.frames())
    finally:
        decoder.close()
    video = [f for f in frames if f.kind == "video"]
    audio = [f for f in frames if f.kind == "audio"]
    assert len(video) >= 49
    assert video[0].width == 320 and video[0].height == 180
    assert len(video[0].data) == 320 * 180 * 3 // 2
    assert sum(f.samples for f in audio) == pytest.approx(96000, abs=1000)
    assert abs(video[-1].time - (audio[-1].time + audio[-1].samples / 48000)) < .1
    assert all(len(f.data) == f.samples * 4 for f in audio)


def test_seek_discards_preroll(clip):
    decoder = Decoder({"url": str(clip), "seek": 1})
    try:
        frames = list(decoder.frames())
    finally:
        decoder.close()
    assert all(f.time >= 0 for f in frames)
    assert max(f.time for f in frames) < 1.1


@pytest.mark.parametrize("source,expected", [((3840, 2160), (1920, 1080)), ((1920, 800), (1920, 800)), ((640, 480), (640, 480))])
def test_preserves_aspect_ratio_and_caps_resolution(source, expected):
    assert dimensions(*source) == expected


def test_volume_is_stereo_safe_and_does_not_overflow():
    samples = np.array([32767, -32768, 10000, -10000], dtype=np.int16).tobytes()
    assert np.frombuffer(scale_audio(samples, 0), dtype=np.int16).tolist() == [0, 0, 0, 0]
    assert np.frombuffer(scale_audio(samples, 50), dtype=np.int16).tolist() == [16383, -16384, 5000, -5000]


def test_worker_entrypoint_binds_room_to_running_loop(monkeypatch):
    from cinema.worker_entry import main

    async def inspect_room(worker):
        assert worker.room._loop is asyncio.get_running_loop()

    monkeypatch.setattr(Worker, "run", inspect_room)
    monkeypatch.setattr("cinema.worker_entry.logging.disable", lambda *_: None)
    monkeypatch.setattr("cinema.worker_entry.confine_worker", lambda *_: None)
    monkeypatch.setattr("sys.argv", ["cinema.worker_entry"])
    main()


async def test_worker_pause_and_clock_with_real_decode(clip, monkeypatch):
    events = []
    monkeypatch.setattr("cinema.worker.emit", lambda event, **kw: events.append({"event": event, **kw}))
    worker = Worker()

    class Sink:
        async def push(self, frame, timestamp):
            pass
        async def wait_for_playout(self):
            pass
        def reset(self):
            pass

    async def tracks(decoder):
        worker.sync = Sink()
    monkeypatch.setattr(worker, "tracks", tracks)
    task = asyncio.create_task(worker.play({"url": str(clip)}, 1))
    await asyncio.sleep(.3)
    worker.pause()
    position = worker.position
    await asyncio.sleep(.2)
    assert worker.position == position
    worker.resume()
    await asyncio.wait_for(task, 5)
    assert events[-1]["event"] == "ended"
    assert worker.position == pytest.approx(2, abs=.1)


@pytest.mark.parametrize("codec,rate", [("libx264", 24), ("libx265", 60)])
def test_h264_hevc_and_high_frame_rate(tmp_path, codec, rate):
    ffmpeg = shutil.which("ffmpeg")
    if not ffmpeg:
        pytest.skip("FFmpeg CLI needed to generate codec fixtures; CI installs it")
    path = tmp_path / "codec.mkv"
    command = [ffmpeg, "-v", "error", "-f", "lavfi", "-i", f"testsrc2=size=320x180:rate={rate}",
               "-f", "lavfi", "-i", "sine=frequency=440:sample_rate=48000", "-t", "1",
               "-c:v", codec, "-preset", "ultrafast", "-c:a", "aac", "-y", str(path)]
    subprocess.run(command, check=True, capture_output=True, timeout=30)
    decoder = Decoder({"url": str(path)})
    try:
        assert decoder.fps <= 30
        frames = list(decoder.frames())
    finally:
        decoder.close()
    videos = [x for x in frames if x.kind == "video"]
    assert len(videos) <= 32
    assert .9 <= videos[-1].time <= 1.1


@pytest.mark.parametrize("rate", [Fraction(24), Fraction(24000, 1001), Fraction(25), Fraction(30000, 1001)])
def test_rounded_container_timestamps_preserve_every_movie_frame(tmp_path, rate):
    path = tmp_path / "cadence.mkv"
    # Lossless pixels identify frames; Matroska rounds their PTS to milliseconds.
    with av.open(str(path), "w") as container:
        stream = container.add_stream("ffv1", rate=rate)
        stream.width, stream.height, stream.pix_fmt = 64, 48, "yuv420p"
        for index in range(120):
            pixels = np.full((72, 64), 128, dtype=np.uint8)
            pixels[:48] = index + 20
            frame = av.VideoFrame.from_ndarray(pixels, format="yuv420p")
            frame.pts, frame.time_base = index, 1 / rate
            for packet in stream.encode(frame):
                container.mux(packet)
        for packet in stream.encode(None):
            container.mux(packet)
    decoder = Decoder({"url": str(path)})
    try:
        video = [frame for frame in decoder.frames() if frame.kind == "video"]
    finally:
        decoder.close()
    assert [frame.data[0] for frame in video] == list(range(20, 140))
    assert [frame.time for frame in video] == pytest.approx([index / float(rate) for index in range(120)])


def test_vod_hls_seek_preserves_picture_and_timeline(tmp_path):
    path = tmp_path / "master.m3u8"
    with av.open(str(path), "w", format="hls", options={
            "hls_time": "1", "hls_list_size": "0", "hls_playlist_type": "vod"}) as output:
        video = output.add_stream("libx264", rate=25)
        video.width, video.height, video.pix_fmt = 160, 90, "yuv420p"
        video.codec_context.gop_size = 25
        video.options = {"preset": "ultrafast", "crf": "18"}
        for index in range(150):
            frame = av.VideoFrame.from_ndarray(
                np.full((90, 160, 3), 20 + index, dtype=np.uint8), format="rgb24")
            frame.pts, frame.time_base = index, Fraction(1, 25)
            for packet in video.encode(frame):
                output.mux(packet)
        for packet in video.encode(None):
            output.mux(packet)

    decoder = Decoder({"url": str(path), "seek": 4.4, "offset": 4.4})
    try:
        frames = list(decoder.frames())
    finally:
        decoder.close()
    assert frames and all(f.time >= 0 for f in frames)
    assert frames[0].time < .05
    assert frames[-1].time == pytest.approx(1.56, abs=.05)
    # Verify actual content, not just a relabeled clock: at 4.4s RGB is ~130.
    assert np.frombuffer(frames[0].data[:160 * 90], dtype=np.uint8).mean() > 110


def test_explicit_missing_audio_does_not_silently_play_another_track(clip):
    with pytest.raises(ValueError, match="Selected audio stream"):
        Decoder({"url": str(clip), "audio_index": 99})


def test_audio_ordinal_wins_over_jellyfin_global_index(clip):
    # Jellyfin can report audio index 2 even when the file's audio index is 1.
    decoder = Decoder({"url": str(clip), "audio_index": 2, "audio_ordinal": 0})
    try:
        assert decoder.audio.index == 1
        assert any(frame.kind == "audio" for frame in decoder.frames())
    finally:
        decoder.close()
