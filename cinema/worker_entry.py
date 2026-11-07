"""Apply confinement before importing native media libraries or starting threads."""
import argparse
import asyncio
import json
import logging
import sys

from cinema.sandbox import confine_worker


async def run(local_file=None):
    confine_worker(local_file)
    import av

    from cinema.worker import run_worker
    av.logging.set_level(av.logging.PANIC)
    await run_worker()


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--local-file")
    args = parser.parse_args()
    logging.disable(logging.CRITICAL)
    try:
        asyncio.run(run(args.local_file))
    except Exception:
        print(json.dumps({"event": "fatal", "message": "Media worker initialization or sandbox failed."}), flush=True)
        sys.exit(1)


if __name__ == "__main__":
    main()
