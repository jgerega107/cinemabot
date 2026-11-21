"""Run inside the built Linux image; contains no upstream credentials or live tests."""
import importlib.util
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

import av
import httpx

assert os.getuid() == 10001
assert shutil.which("node") is None, "Node must not ship in the runtime image"
assert all(importlib.util.find_spec(name) is None for name in ("brotli", "brotlicffi"))
assert "br" not in httpx.Client().headers.get("accept-encoding", "").split(", ")
package = subprocess.run(["dpkg-query", "-W", "-f=${Version}", "libbrotli1"], capture_output=True, text=True)
if package.returncode == 0 and package.stdout:
    release = dict(line.split("=", 1) for line in Path("/etc/os-release").read_text().splitlines() if "=" in line)
    minimum = {"bookworm": "1.0.9-2+deb12u1", "trixie": "1.1.0-2+deb13u1"}[release["VERSION_CODENAME"].strip('"')]
    subprocess.run(["dpkg", "--compare-versions", package.stdout.strip(), "ge", minimum], check=True)

with tempfile.TemporaryDirectory(prefix="cinema-check-") as directory:
    root = Path(directory)
    secret = root / "backend-secret"
    secret.write_text("must not be readable by the worker")
    clip = root / "clip.mkv"
    with av.open(str(clip), "w") as container:
        stream = container.add_stream("mpeg4", rate=25)
        stream.width, stream.height, stream.pix_fmt = 320, 180, "yuv420p"
        for index in range(5):
            frame = av.VideoFrame(320, 180, "yuv420p")
            for plane in frame.planes:
                plane.update(bytes(plane.buffer_size))
            frame.pts = index
            for packet in stream.encode(frame):
                container.mux(packet)
        for packet in stream.encode(None):
            container.mux(packet)
    child = r'''
import asyncio
import os
import socket
import subprocess
import sys
from pathlib import Path
from cinema.sandbox import confine_worker

async def check():
    confine_worker(sys.argv[1])
    for path in (sys.argv[2], f"/proc/{os.getppid()}/environ"):
        try:
            Path(path).read_bytes()
        except PermissionError:
            pass
        else:
            raise AssertionError("Worker read backend data")
    for operation in (lambda: os.kill(os.getppid(), 0),
                      lambda: subprocess.run([sys.executable, "-c", "pass"], check=True),
                      lambda: socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)):
        try:
            operation()
        except PermissionError:
            pass
        else:
            raise AssertionError("Worker escaped process restrictions")
    from cinema.media import Decoder
    from livekit import rtc
    # Check the real wheel's encoder API inside the confined process, even on CPU-only CI.
    for backend in (rtc.VideoEncoderBackend.ENCODER_BACKEND_VAAPI,
                    rtc.VideoEncoderBackend.ENCODER_BACKEND_NVENC):
        options = rtc.TrackPublishOptions(video_codec=rtc.VideoCodec.H264, video_encoder=backend)
        assert options.video_encoder == backend
    decoder = await asyncio.to_thread(Decoder, {"url": sys.argv[1]})
    try:
        frames = await asyncio.to_thread(lambda: list(decoder.frames()))
        assert any(f.kind == "video" for f in frames)
    finally:
        decoder.close()
    audio = rtc.AudioSource(48000, 2)
    video = rtc.VideoSource(320, 180)
    await audio.capture_frame(rtc.AudioFrame.create(48000, 2, 960))
    await audio.aclose()
    await video.aclose()
    assert socket.getaddrinfo("localhost", 80)
    print("Worker confinement and native media checks passed")

asyncio.run(check())
'''
    subprocess.run([sys.executable, "-c", child, str(clip), str(secret)], check=True, timeout=30)
print("Runtime contains no Node/Python Brotli decoder; security smoke checks passed")
