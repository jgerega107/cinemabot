import asyncio
import json
import logging
from contextlib import asynccontextmanager
from typing import Annotated

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
    http = httpx.AsyncClient(timeout=20, follow_redirects=False)
    fluxer = Fluxer(config, http)
    jellyfin = Jellyfin(config, http)
    playback = Playback(config, fluxer, jellyfin)
    auth = Auth(config, fluxer, http)

    @asynccontextmanager
    async def lifespan(app):
        tasks = []
        if start_services:
            tasks = [asyncio.create_task(fluxer.run()), asyncio.create_task(jellyfin.monitor())]
            playback.start()
        try:
            yield
        finally:
            await playback.close()
            await fluxer.close()
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            await http.aclose()

    app = FastAPI(title="Fluxer Cinema", lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)
    app.state.auth, app.state.fluxer, app.state.jellyfin, app.state.playback = auth, fluxer, jellyfin, playback

    @app.exception_handler(Problem)
    async def problem_handler(request, exc):
        return JSONResponse({"error": exc.message}, status_code=exc.status)

    @app.exception_handler(httpx.HTTPError)
    async def network_handler(request, exc):
        return JSONResponse({"error": "An upstream service is unavailable. Try again shortly."}, status_code=503)

    @app.middleware("http")
    async def boundaries(request: Request, call_next):
        if request.method not in {"GET", "HEAD", "POST"}:
            return JSONResponse({"error": "Method not supported."}, status_code=405)
        if request.method == "POST":
            if request.headers.get("origin") != config.public_origin:
                return JSONResponse({"error": "Open the player to perform this action."}, status_code=403)
            body = bytearray()
            async for chunk in request.stream():
                body.extend(chunk)
                if len(body) > 16384:
                    return JSONResponse({"error": "Request is too large."}, status_code=413)
            request._body = bytes(body)
        try:
            response = await call_next(request)
        except Exception as e:
            log.error("Request failed (%s)", type(e).__name__)
            response = JSONResponse({"error": "The service could not complete this request."}, status_code=500)
        response.headers.update({
            "Cache-Control": "no-store", "X-Content-Type-Options": "nosniff", "Referrer-Policy": "no-referrer",
            "Content-Security-Policy": "default-src 'self'; script-src 'self'; style-src 'self'; "
            "img-src 'self' data:; connect-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'self'",
        })
        return response

    @app.get("/healthz")
    async def health():
        return {"ok": True}

    @app.get("/readyz")
    async def ready():
        ok = fluxer.ready and jellyfin.ready
        return JSONResponse({"ok": ok, "fluxer": fluxer.ready, "jellyfin": jellyfin.ready}, status_code=200 if ok else 503)

    @app.get("/api/meta")
    async def meta():
        return {"instance": config.fluxer_origin, "title": "Fluxer Cinema"}

    @app.get("/auth/login")
    async def login():
        response = RedirectResponse("/", status_code=303)
        response.headers["location"] = auth.login(response)
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
        return await jellyfin.libraries()

    @app.get("/api/items")
    async def items(request: Request, q: str = Query(default="", max_length=200),
                    parent: str | None = Query(default=None, pattern=r"^[a-fA-F0-9-]{32,36}$"),
                    start: int = Query(default=0, ge=0, le=100000)):
        await auth.require(request, fresh=True)
        return await jellyfin.browse(q, parent, start)

    @app.get("/api/items/{item_id}")
    async def item(request: Request, item_id: ItemId):
        await auth.require(request, fresh=True)
        return await jellyfin.item(item_id)

    @app.get("/api/artwork/{item_id}")
    async def artwork(request: Request, item_id: ItemId):
        await auth.require(request)
        content, kind = await jellyfin.artwork(item_id)
        return Response(content, media_type=kind)

    @app.get("/api/player")
    async def player(request: Request):
        user = await auth.require(request, fresh=True)
        return playback.snapshot(user["id"])

    @app.post("/api/action")
    async def action(request: Request, command: Action):
        user = await auth.require(request, fresh=True)
        auth.rate_limit(user["id"])
        await playback.action(user, command)
        return playback.snapshot(user["id"])

    streams: dict[str, int] = {}

    @app.get("/api/events")
    async def events(request: Request):
        user = await auth.require(request, fresh=True)
        uid = user["id"]
        if streams.get(uid, 0) >= 4:
            raise Problem(429, "Too many player tabs. Close another tab and retry.")
        streams[uid] = streams.get(uid, 0) + 1

        async def stream():
            try:
                while not await request.is_disconnected():
                    try:
                        await auth.require(request)
                    except Problem as e:
                        yield "event: session\ndata: " + json.dumps({"error": e.message, "status": e.status}) + "\n\n"
                        return
                    yield "event: player\ndata: " + json.dumps(playback.snapshot(uid)) + "\n\n"
                    await asyncio.sleep(1)
            finally:
                streams[uid] -= 1
                if not streams[uid]:
                    del streams[uid]
        return StreamingResponse(stream(), media_type="text/event-stream",
                                 headers={"X-Accel-Buffering": "no", "Cache-Control": "no-store"})

    if (config.static_dir / "assets").is_dir():
        app.mount("/assets", StaticFiles(directory=config.static_dir / "assets"), name="assets")

    @app.get("/")
    async def index():
        if (config.static_dir / "index.html").is_file():
            return FileResponse(config.static_dir / "index.html")
        return JSONResponse({"message": "Build the frontend with npm ci && npm run build in web/."}, status_code=503)

    return app
