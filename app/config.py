"""Application configuration (environment variables)."""

from __future__ import annotations

import os
from dataclasses import dataclass


@dataclass(frozen=True)
class Settings:
    database_url: str
    host: str
    port: int
    max_query_days: int
    reservation_max_days_ahead: int
    reservation_max_duration_minutes: int

    @staticmethod
    def load() -> "Settings":
        url = os.getenv("DATABASE_URL", "").strip()
        if not url:
            host = os.getenv("POSTGRES_HOST", "db")
            port = int(os.getenv("POSTGRES_PORT", "5432"))
            name = os.getenv("POSTGRES_DB", "recycle")
            user = os.getenv("POSTGRES_USER", "recycle")
            password = os.getenv("POSTGRES_PASSWORD", "recycle")
            url = f"postgresql://{user}:{password}@{host}:{port}/{name}"
        return Settings(
            database_url=url,
            host=os.getenv("APP_HOST", "0.0.0.0"),
            port=int(os.getenv("APP_PORT", "8000")),
            max_query_days=int(os.getenv("MAX_QUERY_DAYS", "370")),
            reservation_max_days_ahead=int(
                os.getenv("RESERVATION_MAX_DAYS_AHEAD", "30")
            ),
            reservation_max_duration_minutes=int(
                os.getenv("RESERVATION_MAX_DURATION_MINUTES", "120")
            ),
        )


settings = Settings.load()
