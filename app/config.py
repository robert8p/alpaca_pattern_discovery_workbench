from __future__ import annotations

from functools import lru_cache

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    database_url: str = Field(alias="DATABASE_URL")
    app_username: str = Field(default="admin", alias="APP_USERNAME")
    app_password: str = Field(default="change-me", alias="APP_PASSWORD")
    auto_migrate: bool = Field(default=True, alias="AUTO_MIGRATE")
    worker_poll_seconds: float = Field(default=3.0, alias="WORKER_POLL_SECONDS")
    worker_stale_seconds: int = Field(default=300, alias="WORKER_STALE_SECONDS")
    max_job_attempts: int = Field(default=3, alias="MAX_JOB_ATTEMPTS")
    database_statement_timeout_seconds: int = Field(default=600, alias="DATABASE_STATEMENT_TIMEOUT_SECONDS")
    feature_batch_wall_timeout_seconds: int = Field(default=660, alias="FEATURE_BATCH_WALL_TIMEOUT_SECONDS")
    feature_min_symbol_batch_size: int = Field(default=1, alias="FEATURE_MIN_SYMBOL_BATCH_SIZE")
    feature_cancel_grace_seconds: int = Field(default=15, alias="FEATURE_CANCEL_GRACE_SECONDS")
    log_level: str = Field(default="INFO", alias="LOG_LEVEL")


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()
