import ipaddress
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
    jellyfin_force_transcode: bool = True
    video_bitrate: int = Field(default=6_000_000, ge=500_000, le=20_000_000)
    empty_channel_seconds: int = Field(default=60, ge=5, le=600)
    session_seconds: int = Field(default=28800, ge=60, le=86400)
    static_dir: Path = Path("web/dist")
    allow_insecure_local: bool = False
    trusted_proxy_cidrs: str = ""

    @field_validator("trusted_proxy_cidrs")
    @classmethod
    def proxies(cls, value: str) -> str:
        for network in value.split(","):
            if network.strip():
                parsed = ipaddress.ip_network(network.strip(), strict=False)
                if parsed.prefixlen == 0:
                    raise ValueError("Trust only the actual reverse proxy addresses, not the entire internet")
        return value

    @property
    def proxy_networks(self):
        return [ipaddress.ip_network(x.strip(), strict=False) for x in self.trusted_proxy_cidrs.split(",") if x.strip()]

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
            host = urlsplit(self.public_origin).hostname
            local = host == "localhost"
            try:
                address = ipaddress.ip_address(host)
            except ValueError:
                pass
            else:
                # Explicit LAN ranges: is_private also includes non-LAN special-use addresses.
                local = address.is_loopback or any(address in ipaddress.ip_network(network) for network in (
                    "10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16", "fc00::/7",
                ))
            if not self.allow_insecure_local or not local:
                raise ValueError("PUBLIC_ORIGIN requires HTTPS (localhost and private LAN IPs can opt out "
                                 "with ALLOW_INSECURE_LOCAL=true)")
        if not self.fluxer_origin.startswith("https://") and not self.allow_insecure_local:
            raise ValueError("FLUXER_ORIGIN requires HTTPS")

    @property
    def channel_ids(self) -> set[str]:
        return {x.strip() for x in self.fluxer_channel_ids.split(",") if x.strip()}
