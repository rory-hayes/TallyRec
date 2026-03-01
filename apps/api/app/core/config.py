from __future__ import annotations

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    database_url: str = "postgresql://postgres:postgres@127.0.0.1:5432/postgres"
    app_env: str = "development"
    cors_allow_origins: list[str] = Field(default_factory=lambda: ["http://localhost:3000", "http://127.0.0.1:3000"])
    cors_allow_origin_regex: str | None = r"https://.*\.vercel\.app"
    cors_allow_credentials: bool = True

    model_config = SettingsConfigDict(env_prefix="", extra="ignore")

    @field_validator("cors_allow_origins", mode="before")
    @classmethod
    def _parse_cors_allow_origins(cls, value: object) -> object:
        if isinstance(value, str):
            parts = [item.strip() for item in value.split(",")]
            return [item for item in parts if item]
        return value


settings = Settings()
