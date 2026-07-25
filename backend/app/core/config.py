import sys
from functools import cached_property
from typing import Any

from pydantic import HttpUrl, PostgresDsn, RedisDsn, field_validator
from pydantic.networks import AnyHttpUrl
from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    PROJECT_NAME: str = "Poulse Kora Backend"

    SENTRY_DSN: HttpUrl | None = None

    API_PATH: str = "/api/v1"

    ACCESS_TOKEN_EXPIRE_MINUTES: int = 7 * 24 * 60  # 7 days

    # Number of posts a user must review (forward or drop) before they can create one.
    # Superusers still bypass gating; no longer gates posting (replaced by the token
    # economy, see the FEED_* settings below), but kept for reference/compatibility.
    RELAY_REVIEW_GATE: int = 5

    # --- Redis-backed feed distribution algorithm ---
    # Per-user review-queue capacity. A user is in the `free_queue` set while their
    # queue holds fewer than this many post_ids.
    FEED_QUEUE_MAX_SLOTS: int = 20
    # Recipients (K) each operation fans a post out to.
    FEED_FANOUT: int = 3
    # Dynamic admission price for creating an original post, as a function of the
    # operation-queue length: clamp(MIN + len(ops) // STEP_ITEMS, MIN, MAX).
    FEED_PRICE_MIN: int = 1
    FEED_PRICE_MAX: int = 5
    FEED_PRICE_STEP_ITEMS: int = 20
    # Seconds an undeliverable operation (no free recipient) waits before retry.
    FEED_RETRY_INTERVAL_SECONDS: int = 20

    BACKEND_CORS_ORIGINS: list[AnyHttpUrl] = []

    TEST_DATABASE_URL: PostgresDsn | None = None
    DATABASE_URL: PostgresDsn

    @field_validator("DATABASE_URL", mode="before")
    @classmethod
    def build_test_database_url(cls, v: str | None, info: dict[str, Any]) -> str:
        """Overrides DATABASE_URL with TEST_DATABASE_URL in test environment."""
        if v is None:
            raise ValueError("DATABASE_URL cannot be None")

        if "pytest" in sys.modules:
            test_url = info.data.get("TEST_DATABASE_URL")
            if not test_url:
                raise ValueError(
                    "pytest detected, but TEST_DATABASE_URL is not set in environment"
                )
            v = str(test_url)

        return v.replace("postgres://", "postgresql://")

    @cached_property
    def ASYNC_DATABASE_URL(self):
        """Builds ASYNC_DATABASE_URL from DATABASE_URL."""
        v = str(self.DATABASE_URL)
        return v.replace("postgresql", "postgresql+asyncpg", 1) if v else v

    TEST_REDIS_URL: RedisDsn | None = None
    REDIS_URL: RedisDsn

    @field_validator("REDIS_URL", mode="before")
    @classmethod
    def build_test_redis_url(cls, v: str | None, info: dict[str, Any]) -> str:
        """Overrides REDIS_URL with TEST_REDIS_URL in test environment."""
        if v is None:
            raise ValueError("REDIS_URL cannot be None")

        if "pytest" in sys.modules:
            test_url = info.data.get("TEST_REDIS_URL")
            if not test_url:
                raise ValueError(
                    "pytest detected, but TEST_REDIS_URL is not set in environment"
                )
            v = str(test_url)

        return v

    SECRET_KEY: str


settings = Settings()
