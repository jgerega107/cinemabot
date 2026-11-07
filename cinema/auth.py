import base64
import hashlib
import hmac
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
        self.flow_secret = secrets.token_bytes(32)
        self.used_flows: dict[str, float] = {}
        self.secure = config.public_origin.startswith("https://")
        self.cookie = "__Host-cinema" if self.secure else "cinema-local"
        self.flow_cookie = self.cookie + "-login"

    def prune(self):
        now = time.monotonic()
        self.sessions = {k: v for k, v in self.sessions.items() if v.expires > now}
        self.used_flows = {k: v for k, v in self.used_flows.items() if v > time.time()}

    def mac(self, purpose: str, payload: str) -> str:
        digest = hmac.digest(self.flow_secret, (purpose + ":" + payload).encode(), "sha256")
        return base64.urlsafe_b64encode(digest).decode().rstrip("=")

    def flow(self, value: str):
        if len(value) > 256 or not value.isascii():
            return None
        parts = value.split(".")
        if len(parts) != 3 or len(parts[0]) != 43 or not parts[1].isdigit():
            return None
        payload = ".".join(parts[:2])
        expires = int(parts[1])
        if not secrets.compare_digest(parts[2], self.mac("cookie", payload)) or not 0 < expires - time.time() <= 300:
            return None
        if payload in self.used_flows:
            return None
        return payload, expires

    def set_cookie(self, response: Response, name: str, value: str, seconds: int):
        response.set_cookie(name, value, max_age=seconds, secure=self.secure,
                            httponly=True, samesite="lax", path="/")

    def login(self, request: Request, response: Response) -> str:
        self.prune()
        if not self.fluxer.api or not self.fluxer.webapp:
            raise Problem(503, "Fluxer discovery is unavailable. Try again shortly.")
        # Unauthenticated starts allocate no server-side pending-flow entries.
        existing = self.flow(request.cookies.get(self.flow_cookie, ""))
        expires = int(time.time()) + 300
        payload, expires = existing or (secrets.token_urlsafe(32) + "." + str(expires), expires)
        flow = payload + "." + self.mac("cookie", payload)
        state, verifier = self.mac("state", payload), self.mac("pkce", payload)
        self.set_cookie(response, self.flow_cookie, flow, max(1, int(expires - time.time())))
        challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).decode().rstrip("=")
        return self.fluxer.webapp + "/oauth2/authorize?" + urlencode({
            "client_id": self.config.fluxer_client_id, "redirect_uri": self.config.public_origin + "/auth/callback",
            "response_type": "code", "scope": "identify", "state": state,
            "code_challenge": challenge, "code_challenge_method": "S256"})

    async def callback(self, request: Request, response: Response):
        flow = self.flow(request.cookies.get(self.flow_cookie, ""))
        self.set_cookie(response, self.flow_cookie, "", 0)
        state = request.query_params.get("state", "")
        if not flow or not state.isascii() or not secrets.compare_digest(self.mac("state", flow[0]), state):
            raise Problem(400, "Sign-in expired. Return to the player and try again.")
        code = request.query_params.get("code", "")
        if not code or len(code) > 256:
            raise Problem(400, "Sign-in was cancelled or returned an invalid code.")
        self.prune()
        if len(self.used_flows) >= 4096:
            raise Problem(429, "Too many sign-in callbacks. Try again shortly.")
        self.used_flows[flow[0]] = flow[1]
        token_response = await self.http.post(self.fluxer.api + "/oauth2/token", data={
            "grant_type": "authorization_code", "code": code, "code_verifier": self.mac("pkce", flow[0]),
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

    def local_session(self, request: Request) -> Session:
        sid = request.cookies.get(self.cookie, "")
        session = self.sessions.get(sid)
        if not session or session.expires <= time.monotonic():
            self.sessions.pop(sid, None)
            raise Problem(401, "Sign in with Fluxer.")
        return session

    async def require(self, request: Request, *, fresh: bool = False) -> dict:
        session = self.local_session(request)
        sid = request.cookies.get(self.cookie, "")
        now = time.monotonic()
        if fresh or now - session.checked >= 15:
            try:
                session.user = await self.fluxer.member(session.user["id"])
            except Problem as e:
                if e.status == 403:
                    self.sessions.pop(sid, None)
                raise
            session.checked = now
        return session.user

    def logout(self, request: Request, response: Response):
        self.sessions.pop(request.cookies.get(self.cookie, ""), None)
        self.set_cookie(response, self.cookie, "", 0)
