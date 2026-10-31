"""Application settings loaded from environment variables.

All values come from PROTOCOL.md §8. Use :func:`get_settings` to obtain a
cached singleton — Pydantic-Settings parses the environment only once.
"""
from __future__ import annotations

from functools import lru_cache
from urllib.parse import urlparse

from pydantic import field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Strongly-typed view of the runtime configuration."""

    model_config = SettingsConfigDict(
        env_file=None,
        case_sensitive=False,
        extra="ignore",
    )

    # --- Database -----------------------------------------------------------
    DATABASE_URL: str

    # --- Auth ---------------------------------------------------------------
    BEN_TUNNEL_SECRET: str
    ADMIN_EMAIL: str
    ADMIN_BOOTSTRAP_PASSWORD: str
    ADMIN_BOOTSTRAP_FIRST_NAME: str = "Admin"

    # --- Limits (defaults from PROTOCOL.md §6) ------------------------------
    MAX_REQUEST_BYTES: int = 5 * 1024 * 1024
    MAX_RESPONSE_BYTES: int = 5 * 1024 * 1024
    HTTP_TIMEOUT_SECONDS: int = 30
    MAX_WS_CONNECTIONS: int = 200

    # --- Public surface -----------------------------------------------------
    PUBLIC_BASE_URL: str = "https://bentunnel.onrender.com"
    KEEPALIVE_URL: str = "https://bentunnel.onrender.com/health"

    # --- Cookie / session tuning -------------------------------------------
    SESSION_COOKIE_NAME: str = "bentunnel_session"
    CSRF_COOKIE_NAME: str = "bentunnel_csrf"
    SESSION_TTL_DAYS: int = 7
    TUNNEL_TOKEN_TTL_SECONDS: int = 300

    # --- Tunnel constraints (PROTOCOL.md §6) -------------------------------
    MAX_TUNNELS_PER_USER: int = 1

    # --- Computed at access time -------------------------------------------
    @property
    def ws_url(self) -> str:
        """WebSocket URL derived from PUBLIC_BASE_URL.

        ``https://`` becomes ``wss://`` and ``http://`` becomes ``ws://``.
        """
        base = self.PUBLIC_BASE_URL
        if base.startswith("https://"):
            return "wss://" + base[len("https://"):] + "/ws"
        if base.startswith("http://"):
            return "ws://" + base[len("http://"):] + "/ws"
        return base + "/ws"

    @field_validator("DATABASE_URL")
    @classmethod
    def _validate_db_url(cls, v: str) -> str:
        if not v.startswith("mysql+asyncmy://"):
            raise ValueError(
                "DATABASE_URL must start with 'mysql+asyncmy://' "
                f"(got: {v[:40]}…)"
            )
        # Make sure charset=utf8mb4 is present (Alwaysdata requirement).
        return v

    @field_validator("BEN_TUNNEL_SECRET")
    @classmethod
    def _validate_secret(cls, v: str) -> str:
        if len(v) < 32:
            raise ValueError(
                "BEN_TUNNEL_SECRET must be at least 32 chars long"
            )
        return v


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Return the cached :class:`Settings` singleton."""
    return Settings()  # type: ignore[call-arg]
