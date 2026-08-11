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
    feature_db_conflict_retries: int = Field(default=5, alias="FEATURE_DB_CONFLICT_RETRIES")
    discovery_statement_timeout_seconds: int = Field(default=180, alias="DISCOVERY_STATEMENT_TIMEOUT_SECONDS")
    discovery_wall_timeout_seconds: int = Field(default=210, alias="DISCOVERY_WALL_TIMEOUT_SECONDS")
    discovery_cancel_grace_seconds: int = Field(default=15, alias="DISCOVERY_CANCEL_GRACE_SECONDS")
    discovery_query_retries: int = Field(default=3, alias="DISCOVERY_QUERY_RETRIES")
    robustness_initial_symbol_shards: int = Field(default=4, alias="ROBUSTNESS_INITIAL_SYMBOL_SHARDS")
    robustness_query_retries: int = Field(default=3, alias="ROBUSTNESS_QUERY_RETRIES")
    log_level: str = Field(default="INFO", alias="LOG_LEVEL")


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()
