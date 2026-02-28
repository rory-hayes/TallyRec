from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    database_url: str = "postgresql://postgres:postgres@127.0.0.1:5432/postgres"
    app_env: str = "development"

    model_config = SettingsConfigDict(env_prefix="", extra="ignore")


settings = Settings()
