import asyncio
import json
import logging
from contextlib import asynccontextmanager
from typing import Annotated
from urllib.parse import urlsplit

import httpx
from fastapi import FastAPI, Path, Query, Request
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse, Response, StreamingResponse
from fastapi.staticfiles import StaticFiles

from cinema.auth import Auth
from cinema.config import Settings
from cinema.errors import Problem
from cinema.fluxer import Fluxer
from cinema.jellyfin import Jellyfin
from cinema.playback import Action, Playback
from cinema.sandbox import protect_process
from cinema.security import Limits, ResponseCache, client_address, reject_brotli

log = logging.getLogger(__name__)
ItemId = Annotated[str, Path(pattern=r"^[a-fA-F0-9-]{32,36}$")]


def create_app(config: Settings | None = None, *, start_services: bool = True) -> FastAPI:
    cinema_log = logging.getLogger("cinema")
    if not cinema_log.handlers:
        handler = logging.StreamHandler()
        handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s %(message)s"))
        cinema_log.addHandler(handler)
    cinema_log.setLevel(logging.INFO)
    cinema_log.propagate = False
    config = config or Settings()
    config.validate_transport()
    http = httpx.AsyncClient(timeout=20, follow_redirects=False, headers={"Accept-Encoding": "gzip, deflate"},
                             event_hooks={"response": [reject_brotli]})
    fluxer = Fluxer(config, http)
    jellyfin = Jellyfin(config, http)
    playback = Playback(config, fluxer, jellyfin)
    auth = Auth(config, fluxer, http)
    limits = Limits()
    metadata = ResponseCache(max_bytes=4 * 1024 * 1024, ttl=10, size=lambda x: len(json.dumps(x).encode()))
    images = ResponseCache(max_bytes=32 * 1024 * 1024, ttl=10, size=lambda x: len(x[0]))
    proxies = config.proxy_networks

    @asynccontextmanager
    async def lifespan(app):
        protect_process()
        tasks = []
        if start_services:
            tasks = [asyncio.create_task(fluxer.run()), asyncio.create_task(jellyfin.monitor())]
            playback.start()
        try:
            yield
        finally:
            await metadata.close()
            await images.close()
            await playback.close()
            await fluxer.close()
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            await http.aclose()

    app = FastAPI(title="Fluxer Cinema", lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)
    app.state.auth, app.state.fluxer, app.state.jellyfin, app.state.playback = auth, fluxer, jellyfin, playback
    app.state.limits = limits

    @app.exception_handler(Problem)
    async def problem_handler(request, exc):
        return JSONResponse({"error": exc.message.replace("Fluxer", fluxer.brand["name"])}, status_code=exc.status)

    @app.exception_handler(httpx.HTTPError)
    async def network_handler(request, exc):
        return JSONResponse({"error": "An upstream service is unavailable. Try again shortly."}, status_code=503)

    @app.middleware("http")
    async def boundaries(request: Request, call_next):
        async def dispatch():
            if request.method not in {"GET", "HEAD", "POST"}:
                raise Problem(405, "Method not supported.")
            if request.headers.get("content-encoding", "identity").lower() != "identity":
                raise Problem(415, "Compressed request bodies are not supported.")
            if request.method == "POST":
                if request.headers.get("origin") != config.public_origin:
                    raise Problem(403, "Open the player to perform this action.")
                body = bytearray()
                async with asyncio.timeout(5):
                    async for chunk in request.stream():
                        body.extend(chunk)
                        if len(body) > 16384:
                            raise Problem(413, "Request is too large.")
                request._body = bytes(body)
            return await call_next(request)

        try:
            path = request.url.path
            if path.startswith("/auth/"):
                address = client_address(request, proxies)
                limits.rate(("auth", address), per_minute=20, burst=10)
                limits.rate(("auth-global",), per_minute=120, burst=40)
                async with limits.slot("auth", address, total=8, individual=2):
                    response = await dispatch()
            elif path.startswith("/api/") and path != "/api/meta":
                # Local cookie verification and admission happen before any upstream request.
                user_id = auth.local_session(request).user["id"]
                category = "action" if path == "/api/action" else "read"
                limits.rate((category, user_id), per_minute=60 if category == "action" else 240,
                            burst=10 if category == "action" else 80)
                limits.rate((category, "global"), per_minute=300 if category == "action" else 1200,
                            burst=20 if category == "action" else 160)
                async with limits.slot(category, user_id, total=4 if category == "action" else 32,
                                       individual=1 if category == "action" else 16):
                    response = await dispatch()
            else:
                response = await dispatch()
        except Problem as e:
            response = JSONResponse({"error": e.message.replace("Fluxer", fluxer.brand["name"])}, status_code=e.status,
                                    headers={"Retry-After": "5"} if e.status == 429 else None)
        except TimeoutError:
            response = JSONResponse({"error": "Request body timed out."}, status_code=408)
        except Exception as e:
            log.error("Request failed (%s)", type(e).__name__)
            response = JSONResponse({"error": "The service could not complete this request."}, status_code=500)
        image_urls = [fluxer.media, *(fluxer.brand.get(key) for key in
                                      ("logo", "icon", "favicon", "favicon_dark", "backdrop"))]
        image_origins = sorted({f"{urlsplit(url).scheme}://{urlsplit(url).netloc}" for url in image_urls if url})
        # Only successful artwork may be reused by the browser. Keep errors,
        # catalog data, and authenticated API responses out of browser caches.
        response.headers["Cache-Control"] = (
            "private, max-age=3600" if request.url.path.startswith("/api/artwork/")
            and request.method in {"GET", "HEAD"} and response.status_code == 200 else "no-store"
        )
        if request.url.path.startswith("/api/artwork/"):
            response.headers.add_vary_header("Cookie")
        response.headers.update({ "X-Content-Type-Options": "nosniff", "Referrer-Policy": "no-referrer",
            "Content-Security-Policy": "default-src 'self'; script-src 'self'; style-src 'self'; "
            f"img-src 'self' data: {' '.join(image_origins)}; connect-src 'self'; "
            "frame-ancestors 'none'; base-uri 'none'; form-action 'self'",
        })
        return response

    def branded_snapshot(uid):
        snapshot = playback.snapshot(uid)
        snapshot["error"] = snapshot["error"].replace("Fluxer", fluxer.brand["name"])
        return snapshot

    @app.get("/healthz")
    async def health():
        return {"ok": True}

    @app.get("/readyz")
    async def ready():
        ok = fluxer.ready and jellyfin.ready
        return JSONResponse({"ok": ok, "fluxer": fluxer.ready, "jellyfin": jellyfin.ready}, status_code=200 if ok else 503)

    @app.get("/api/meta")
    async def meta():
        return {"instance": config.fluxer_origin, "title": f"{fluxer.brand['name']} Cinema", "branding": fluxer.brand}

    @app.get("/auth/login")
    async def login(request: Request):
        response = RedirectResponse("/", status_code=303)
        response.headers["location"] = auth.login(request, response)
        return response

    @app.get("/auth/callback")
    async def callback(request: Request):
        response = RedirectResponse("/", status_code=303)
        await auth.callback(request, response)
        return response

    @app.post("/auth/logout")
    async def logout(request: Request):
        response = JSONResponse({"ok": True})
        auth.logout(request, response)
        return response

    @app.get("/api/me")
    async def me(request: Request):
        return await auth.require(request, fresh=True)

    @app.get("/api/libraries")
    async def libraries(request: Request):
        await auth.require(request, fresh=True)
        return await cached(metadata, "libraries", jellyfin.libraries)

    @app.get("/api/items")
    async def items(request: Request, q: str = Query(default="", max_length=200),
                    parent: str | None = Query(default=None, pattern=r"^[a-fA-F0-9-]{32,36}$"),
                    start: int = Query(default=0, ge=0, le=100000)):
        await auth.require(request, fresh=True)
        return await cached(metadata, ("browse", q, parent, start), lambda: jellyfin.browse(q, parent, start))

    @app.get("/api/items/{item_id}")
    async def item(request: Request, item_id: ItemId):
        await auth.require(request, fresh=True)
        return await cached(metadata, ("item", item_id), lambda: jellyfin.item(item_id))

    @app.get("/api/artwork/{item_id}")
    async def artwork(request: Request, item_id: ItemId):
        await auth.require(request)
        content, kind = await cached(images, item_id, lambda: jellyfin.artwork(item_id))
        return Response(content, media_type=kind)

    @app.get("/api/player")
    async def player(request: Request):
        user = await auth.require(request, fresh=True)
        return branded_snapshot(user["id"])

    @app.post("/api/action")
    async def action(request: Request, command: Action):
        user = await auth.require(request, fresh=True)
        await playback.action(user, command)
        return branded_snapshot(user["id"])

    streams: dict[str, int] = {}

    @app.get("/api/events")
    async def events(request: Request):
        user = await auth.require(request, fresh=True)
        uid = user["id"]
        if streams.get(uid, 0) >= 4 or sum(streams.values()) >= 64:
            raise Problem(429, "Too many player tabs. Close another tab and retry.")
        streams[uid] = streams.get(uid, 0) + 1

        async def stream():
            try:
                while not await request.is_disconnected():
                    try:
                        await auth.require(request)
                    except (Problem, httpx.HTTPError) as e:
                        if not isinstance(e, Problem):
                            e = Problem(503, "Fluxer is temporarily unavailable.")
                        yield "event: session\ndata: " + json.dumps({"error": e.message.replace("Fluxer", fluxer.brand["name"]), "status": e.status}) + "\n\n"
                        return
                    yield "event: player\ndata: " + json.dumps(branded_snapshot(uid)) + "\n\n"
                    await asyncio.sleep(1)
            finally:
                streams[uid] -= 1
                if not streams[uid]:
                    del streams[uid]
        return StreamingResponse(stream(), media_type="text/event-stream",
                                 headers={"X-Accel-Buffering": "no", "Cache-Control": "no-store"})

    async def cached(cache, key, fetch):
        if not jellyfin.ready:
            raise Problem(503, "Jellyfin account is not verified. Try again shortly.")
        return await cache.get(key, fetch)

    if (config.static_dir / "assets").is_dir():
        app.mount("/assets", StaticFiles(directory=config.static_dir / "assets"), name="assets")

    @app.get("/")
    async def index():
        if (config.static_dir / "index.html").is_file():
            return FileResponse(config.static_dir / "index.html")
        return JSONResponse({"message": "Build the frontend with npm ci && npm run build in web/."}, status_code=503)

    return app
