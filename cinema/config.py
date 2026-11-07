from pathlib import Path
from urllib.parse import urlsplit

from pydantic import Field, SecretStr, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    public_origin: str
    fluxer_origin: str
    fluxer_guild_id: str
    fluxer_channel_ids: str = ""
    fluxer_bot_token: SecretStr
    fluxer_client_id: str
    fluxer_client_secret: SecretStr
    jellyfin_url: str
    jellyfin_user_id: str
    jellyfin_access_token: SecretStr
    video_bitrate: int = Field(default=6_000_000, ge=500_000, le=20_000_000)
    empty_channel_seconds: int = Field(default=60, ge=5, le=600)
    session_seconds: int = Field(default=28800, ge=60, le=86400)
    static_dir: Path = Path("web/dist")
    allow_insecure_local: bool = False

    @field_validator("public_origin", "fluxer_origin")
    @classmethod
    def origin(cls, value: str) -> str:
        p = urlsplit(value)
        if p.scheme not in {"http", "https"} or not p.hostname or p.username or p.password:
            raise ValueError("Expected an HTTP(S) origin without credentials")
        if p.path not in {"", "/"} or p.query or p.fragment:
            raise ValueError("Origin must not contain a path, query, or fragment")
        return value.rstrip("/")

    @field_validator("jellyfin_url")
    @classmethod
    def server_url(cls, value: str) -> str:
        p = urlsplit(value)
        if p.scheme not in {"http", "https"} or not p.hostname or p.username or p.password or p.query or p.fragment:
            raise ValueError("Expected a private Jellyfin HTTP(S) base URL")
        return value.rstrip("/")

    def validate_transport(self) -> None:
        if not self.public_origin.startswith("https://"):
            if not self.allow_insecure_local or urlsplit(self.public_origin).hostname not in {"localhost", "127.0.0.1"}:
                raise ValueError("PUBLIC_ORIGIN requires HTTPS (localhost development can opt out)")
        if not self.fluxer_origin.startswith("https://") and not self.allow_insecure_local:
            raise ValueError("FLUXER_ORIGIN requires HTTPS")

    @property
    def channel_ids(self) -> set[str]:
        return {x.strip() for x in self.fluxer_channel_ids.split(",") if x.strip()}
