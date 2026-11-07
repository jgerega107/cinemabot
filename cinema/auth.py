import base64
import hashlib
import secrets
import time
from dataclasses import dataclass
from urllib.parse import urlencode

from fastapi import Request, Response

from cinema.errors import Problem


@dataclass
class Session:
    user: dict
    expires: float
    checked: float


class Auth:
    def __init__(self, config, fluxer, http):
        self.config, self.fluxer, self.http = config, fluxer, http
        self.sessions: dict[str, Session] = {}
        self.flows: dict[str, dict] = {}
        self.rates: dict[str, list[float]] = {}
        self.secure = config.public_origin.startswith("https://")
        self.cookie = "__Host-cinema" if self.secure else "cinema-local"
        self.flow_cookie = self.cookie + "-login"

    def prune(self):
        now = time.monotonic()
        self.sessions = {k: v for k, v in self.sessions.items() if v.expires > now}
        self.flows = {k: v for k, v in self.flows.items() if v["expires"] > now}
        self.rates = {k: [x for x in v if x > now - 60] for k, v in self.rates.items() if v and v[-1] > now - 60}

    def set_cookie(self, response: Response, name: str, value: str, seconds: int):
        response.set_cookie(name, value, max_age=seconds, secure=self.secure,
                            httponly=True, samesite="lax", path="/")

    def login(self, response: Response) -> str:
        self.prune()
        if not self.fluxer.api or not self.fluxer.webapp:
            raise Problem(503, "Fluxer discovery is unavailable. Try again shortly.")
        if len(self.flows) >= 1000:
            raise Problem(429, "Too many sign-in attempts. Try again shortly.")
        state, verifier, flow = (secrets.token_urlsafe(32) for _ in range(3))
        self.flows[flow] = {"state": state, "verifier": verifier, "expires": time.monotonic() + 300}
        self.set_cookie(response, self.flow_cookie, flow, 300)
        challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).decode().rstrip("=")
        return self.fluxer.webapp + "/oauth2/authorize?" + urlencode({
            "client_id": self.config.fluxer_client_id, "redirect_uri": self.config.public_origin + "/auth/callback",
            "response_type": "code", "scope": "identify", "state": state,
            "code_challenge": challenge, "code_challenge_method": "S256"})

    async def callback(self, request: Request, response: Response):
        flow = self.flows.pop(request.cookies.get(self.flow_cookie, ""), None)
        self.set_cookie(response, self.flow_cookie, "", 0)
        if not flow or flow["expires"] <= time.monotonic() or not secrets.compare_digest(
                flow["state"], request.query_params.get("state", "")):
            raise Problem(400, "Sign-in expired. Return to the player and try again.")
        code = request.query_params.get("code", "")
        if not code or len(code) > 256:
            raise Problem(400, "Sign-in was cancelled or returned an invalid code.")
        token_response = await self.http.post(self.fluxer.api + "/oauth2/token", data={
            "grant_type": "authorization_code", "code": code, "code_verifier": flow["verifier"],
            "redirect_uri": self.config.public_origin + "/auth/callback",
            "client_id": self.config.fluxer_client_id,
            "client_secret": self.config.fluxer_client_secret.get_secret_value()})
        if not token_response.is_success:
            raise Problem(502, "Fluxer could not complete sign-in.")
        token = token_response.json()["access_token"]
        identity = await self.http.get(self.fluxer.api + "/oauth2/userinfo", headers={"Authorization": "Bearer " + token})
        if not identity.is_success:
            raise Problem(502, "Fluxer could not verify your identity.")
        user_id = identity.json().get("id", "")
        if not isinstance(user_id, str) or not user_id.isdigit():
            raise Problem(502, "Fluxer returned an invalid identity.")
        user = await self.fluxer.member(user_id)
        self.prune()
        if len(self.sessions) >= 10000:
            raise Problem(503, "Too many active sessions.")
        self.sessions.pop(request.cookies.get(self.cookie, ""), None)
        sid = secrets.token_urlsafe(32)
        now = time.monotonic()
        self.sessions[sid] = Session(user, now + self.config.session_seconds, now)
        self.set_cookie(response, self.cookie, sid, self.config.session_seconds)
        # No OAuth token is retained: future authorization uses bot membership and voice state.

    async def require(self, request: Request, *, fresh: bool = False) -> dict:
        sid = request.cookies.get(self.cookie, "")
        session = self.sessions.get(sid)
        now = time.monotonic()
        if not session or session.expires <= now:
            self.sessions.pop(sid, None)
            raise Problem(401, "Sign in with Fluxer.")
        if fresh or now - session.checked >= 15:
            try:
                session.user = await self.fluxer.member(session.user["id"])
            except Problem as e:
                if e.status == 403:
                    self.sessions.pop(sid, None)
                raise
            session.checked = now
        return session.user

    def rate_limit(self, user_id: str):
        self.prune()
        now = time.monotonic()
        entries = self.rates.setdefault(user_id, [])
        if len(entries) >= 60:
            raise Problem(429, "Too many playback actions. Try again shortly.")
        entries.append(now)

    def logout(self, request: Request, response: Response):
        self.sessions.pop(request.cookies.get(self.cookie, ""), None)
        self.set_cookie(response, self.cookie, "", 0)
