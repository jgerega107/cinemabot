import asyncio
import contextlib
import json
import os
import sys
import tempfile
from collections.abc import Callable

from cinema.errors import Problem


class Supervisor:
    def __init__(self, on_event: Callable[[dict], None], *, local_file=None):
        self.on_event = on_event
        self.process = None
        self.reader = None
        self.connected = None
        self.closing = False
        self.epoch = 0
        self.local_file = local_file
        self.scratch = None

    async def connect(self, grant: dict, bitrate: int, video_encoder: str = "auto"):
        await self.close()
        self.epoch += 1
        self.closing = False
        self.connected = asyncio.get_running_loop().create_future()
        env = {k: v for k, v in os.environ.items() if k in {
            "PATH", "LANG", "LC_ALL", "TMPDIR", "PYTHONPATH", "LD_LIBRARY_PATH", "SSL_CERT_FILE",
            "SSL_CERT_DIR", "LIBVA_DRIVER_NAME", "LIBVA_DRIVERS_PATH", "XDG_RUNTIME_DIR"}}
        self.scratch = tempfile.TemporaryDirectory(prefix="cinema-worker-")
        env.update(CINEMA_WORKER_TMP=self.scratch.name, TMPDIR=self.scratch.name, PYTHONDONTWRITEBYTECODE="1")
        args = ["--local-file", str(self.local_file)] if self.local_file else []
        self.process = await asyncio.create_subprocess_exec(
            sys.executable, "-m", "cinema.worker_entry", *args, stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL, env=env, limit=65536)
        self.reader = asyncio.create_task(self._read())
        await self.send("connect", grant=grant, bitrate=bitrate, video_encoder=video_encoder)
        try:
            await asyncio.wait_for(self.connected, 25)
        except (TimeoutError, OSError):
            await self.close()
            raise Problem(503, "Media worker could not connect to Fluxer voice.") from None

    async def _read(self):
        process = self.process
        epoch = self.epoch
        try:
            while line := await process.stdout.readline():
                message = json.loads(line)
                if message.get("event") == "connected" and self.connected and not self.connected.done():
                    self.connected.set_result(True)
                else:
                    self.on_event({**message, "worker_epoch": epoch})
            await process.wait()
        except (ValueError, OSError):
            pass
        finally:
            if self.connected and not self.connected.done():
                self.connected.set_exception(Problem(503, "Media worker exited before connecting."))
            if not self.closing:
                self.on_event({"event": "fatal", "worker_epoch": epoch, "message": "Media worker exited unexpectedly."})

    async def send(self, command: str, **data):
        if not self.process or self.process.returncode is not None:
            raise Problem(503, "Media worker is unavailable.")
        self.process.stdin.write((json.dumps({"command": command, **data}) + "\n").encode())
        await self.process.stdin.drain()

    async def close(self):
        self.closing = True
        process = self.process
        if process and process.returncode is None:
            with contextlib.suppress(Exception):
                await self.send("close")
            try:
                await asyncio.wait_for(process.wait(), 3)
            except TimeoutError:
                process.kill()
                await process.wait()
        if self.reader:
            self.reader.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await self.reader
        self.process = self.reader = None
        if self.scratch:
            self.scratch.cleanup()
            self.scratch = None
