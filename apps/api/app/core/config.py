from __future__ import annotations

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    database_url: str = "postgresql://postgres:postgres@127.0.0.1:5432/postgres"
    app_env: str = "development"
    cors_allow_origins: str = "http://localhost:3000,http://127.0.0.1:3000"
    cors_allow_origin_regex: str | None = r"https://.*\.vercel\.app"
    cors_allow_credentials: bool = True

    model_config = SettingsConfigDict(env_prefix="", extra="ignore")

    @property
    def cors_allow_origins_list(self) -> list[str]:
        parts = [item.strip() for item in self.cors_allow_origins.split(",")]
        return [item for item in parts if item]


settings = Settings()
