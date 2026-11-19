"""Typed application settings loaded strictly from environment variables."""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from typing import Literal

from pydantic import Field, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


DEFAULT_DEV_SECRET = "quantico-censo-dev-only-secret-key-change-in-production-2026"


class Settings(BaseSettings):
    """Runtime settings for Quantico Censo backend nodes."""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
    )

    app_name: str = "Quantico Censo"
    app_version: str = "1.0.0"
    app_env: Literal["development", "test", "production"] = "development"
    log_level: str = "INFO"

    database_url: str = "sqlite:///./quantico_censo_dev.db"
    redis_url: str = ""

    jwt_secret_key: str = Field(default=DEFAULT_DEV_SECRET, min_length=32)
    jwt_algorithm: str = "HS256"
    jwt_expire_minutes: int = Field(default=480, ge=5, le=1440)
    jwt_issuer: str = "quantico-censo"

    cors_origins: str = "http://localhost:8095,http://127.0.0.1:8095,http://localhost:5173"
    trusted_proxy_ips: str = "127.0.0.1,::1,10.0.0.0/8,172.16.0.0/12,192.168.0.0/16"
    trust_cloudflare_headers: bool = False

    media_root: Path = Path("./media_storage")
    max_photo_bytes: int = Field(default=5 * 1024 * 1024, ge=65_536)
    max_csv_import_bytes: int = Field(default=10 * 1024 * 1024, ge=65_536)

    server_role: Literal["sync", "read", "all"] = "all"
    auto_seed_demo: bool = True
    geofence_drift_meters: float = Field(default=150.0, ge=10.0, le=5000.0)
    rate_limit_enabled: bool = True

    @field_validator("jwt_algorithm")
    @classmethod
    def _validate_jwt_algorithm(cls, value: str) -> str:
        allowed = {"HS256", "HS384", "HS512"}
        if value not in allowed:
            raise ValueError(f"Unsupported JWT algorithm: {value}")
        return value

    @model_validator(mode="after")
    def _validate_production_secrets(self) -> Settings:
        if self.app_env == "production":
            lowered = self.jwt_secret_key.lower()
            weak_markers = ("replace", "demo", "change_me", "dev-only", "example", "secret-key")
            if self.jwt_secret_key == DEFAULT_DEV_SECRET or any(m in lowered for m in weak_markers):
                raise ValueError("JWT_SECRET_KEY must be set to a strong random value in production.")
        return self

    @property
    def cors_origins_list(self) -> list[str]:
        return [origin.strip().rstrip("/") for origin in self.cors_origins.split(",") if origin.strip()]

    @property
    def trusted_proxy_networks(self) -> list[str]:
        return [item.strip() for item in self.trusted_proxy_ips.split(",") if item.strip()]

    @property
    def is_postgres(self) -> bool:
        return self.database_url.startswith("postgresql")


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Return cached runtime settings."""
    return Settings()
