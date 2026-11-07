"""Operator-run compatibility gate; does not create application credentials."""
import argparse
import asyncio
import contextlib
import json
import time
from pathlib import Path

import httpx

from cinema.config import Settings
from cinema.fluxer import Fluxer
from cinema.jellyfin import Jellyfin
from cinema.media import Decoder
from cinema.supervisor import Supervisor


async def probe(args):
    config = Settings(_env_file=args.env_file)
    config.validate_transport()
    events = asyncio.Queue()
    worker = Supervisor(events.put_nowait)
    source = None
    rows = []
    async with httpx.AsyncClient(timeout=20, follow_redirects=False) as http:
        fluxer, jellyfin = Fluxer(config, http), Jellyfin(config, http)
        ready = asyncio.Event()
        fluxer.on_change = lambda: ready.set() if fluxer.ready else ready.clear()
        task = asyncio.create_task(fluxer.run())
        try:
            async with asyncio.timeout(45):
                await ready.wait()
            grant = await fluxer.join(args.channel)
            await worker.connect(grant, config.video_bitrate)
            if args.item:
                await jellyfin.check()
                source = await jellyfin.resolve(args.item, 0, None, -1)
                spec = source.worker()
            else:
                spec = {"url": str(await asyncio.to_thread(Path(args.file).resolve)), "headers": {}, "seek": 0, "offset": 0}
            generation = 1
            await worker.send("play", source=spec, generation=generation)
            started = time.monotonic()
            published = False
            pauses_done = set()
            while time.monotonic() - started < args.seconds:
                try:
                    event = await asyncio.wait_for(events.get(), 10)
                except TimeoutError:
                    raise RuntimeError("No progress received from media worker") from None
                if event.get("worker_epoch", worker.epoch) != worker.epoch:
                    continue
                if event.get("generation", generation) != generation:
                    continue
                kind = event["event"]
                if kind == "playing":
                    await fluxer.advertise(True)
                    published = True
                    print("Published screen-share tracks. Open Watch Stream in Fluxer to verify picture and sound.")
                    if source:
                        await jellyfin.report(source, "start", 0)
                elif kind in {"error", "fatal", "disconnected"}:
                    raise RuntimeError(event.get("message", "Voice connection interrupted"))
                elif kind == "ended":
                    generation += 1
                    await worker.send("play", source=spec, generation=generation)
                elif kind == "progress":
                    elapsed = time.monotonic() - started
                    rows.append({"elapsed": round(elapsed, 2), **event})
                    if source and len(rows) % 10 == 0:
                        await jellyfin.report(source, "progress", event["position"], event["paused"])
                    if len(rows) % 10 == 0:
                        print(f"{elapsed:.0f}s: {event['fps']} fps; publisher drift {event['av_drift_ms']} ms")
                    if args.exercise_controls:
                        for mark, action in ((30, "pause"), (35, "resume")):
                            if elapsed >= mark and mark not in pauses_done:
                                await worker.send(action)
                                pauses_done.add(mark)
                        if elapsed >= 60 and "seek" not in pauses_done:
                            if source:
                                await jellyfin.release(source, event["position"])
                                source = await jellyfin.resolve(args.item, 10, None, -1)
                                spec = source.worker()
                            else:
                                spec = {**spec, "seek": 10, "offset": 10}
                            generation += 1
                            await worker.send("play", source=spec, generation=generation)
                            pauses_done.add("seek")
            report = {"published": published, "seconds": args.seconds, "samples": rows,
                      "viewer_verified": False, "note": "Publisher metrics only; record receiver A/V sync separately."}
            await asyncio.to_thread(Path(args.output).parent.mkdir, parents=True, exist_ok=True)
            await asyncio.to_thread(Path(args.output).write_text, json.dumps(report, indent=2) + "\n")
            print(f"Publisher probe complete. Report: {args.output}. Viewer verification is still required.")
        finally:
            await worker.close()
            if source:
                await jellyfin.release(source, rows[-1]["position"] if rows else 0)
            await fluxer.close()
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task


def main():
    parser = argparse.ArgumentParser(description="Publish a Jellyfin title or local test clip as a Fluxer bot")
    parser.add_argument("--env-file", default=".env")
    parser.add_argument("--channel", required=True, help="Explicit test voice-channel ID")
    media = parser.add_mutually_exclusive_group(required=True)
    media.add_argument("--item", help="Jellyfin movie or episode ID")
    media.add_argument("--file", help="Local test clip, visible inside the bot container")
    parser.add_argument("--seconds", type=int, default=1800)
    parser.add_argument("--exercise-controls", action="store_true")
    parser.add_argument("--output", default="validation/probe.json")
    args = parser.parse_args()
    if not 1 <= args.seconds <= 86400:
        parser.error("--seconds must be between 1 and 86400")
    if args.file:
        with contextlib.closing(Decoder({"url": args.file})):
            pass
    try:
        asyncio.run(probe(args))
    except (KeyboardInterrupt, Exception) as e:
        # Native/HTTP exceptions may contain credentials. Keep console output sanitized.
        print(f"Probe did not complete ({type(e).__name__}). Check configuration, voice permissions, and server logs.")
        raise SystemExit(1) from None


if __name__ == "__main__":
    main()
