"""Unit tests for the Settings validators that guard test/prod database wiring.

These raise on misconfiguration precisely so a broken environment fails loudly
(wrong DB connected, or a missing TEST_DATABASE_URL under pytest) instead of
silently running tests against the wrong database. Constructed directly rather
than via the module-level `settings` singleton so the invalid states never leak
into the shared object other tests rely on.
"""

import pytest
from pydantic import ValidationError

from app.core.config import Settings


def _base_kwargs(**overrides):
    kwargs = {
        "SECRET_KEY": "test-secret",
        "DATABASE_URL": "postgresql://user:pass@host/db",
        "REDIS_URL": "redis://host:6379/0",
    }
    kwargs.update(overrides)
    return kwargs


class TestDatabaseUrlValidator:
    def test_none_database_url_raises(self):
        with pytest.raises(ValidationError, match="DATABASE_URL cannot be None"):
            Settings(**_base_kwargs(DATABASE_URL=None))

    def test_missing_test_database_url_under_pytest_raises(self):
        # `sys.modules` contains "pytest" for the whole suite, so this always
        # exercises the under-test branch of the validator.
        with pytest.raises(
            ValidationError, match="TEST_DATABASE_URL is not set"
        ):
            Settings(**_base_kwargs(TEST_DATABASE_URL=None))

    def test_test_database_url_present_is_swapped_in(self):
        settings = Settings(
            **_base_kwargs(
                DATABASE_URL="postgresql://prod/db",
                TEST_DATABASE_URL="postgresql://test-host/apptest",
            )
        )
        assert "test-host" in str(settings.DATABASE_URL)


class TestRedisUrlValidator:
    def test_none_redis_url_raises(self):
        with pytest.raises(ValidationError, match="REDIS_URL cannot be None"):
            Settings(**_base_kwargs(REDIS_URL=None))

    def test_missing_test_redis_url_under_pytest_raises(self):
        with pytest.raises(ValidationError, match="TEST_REDIS_URL is not set"):
            Settings(**_base_kwargs(TEST_REDIS_URL=None))

    def test_test_redis_url_present_is_swapped_in(self):
        settings = Settings(
            **_base_kwargs(
                REDIS_URL="redis://prod-host:6379/0",
                TEST_REDIS_URL="redis://test-host:6379/1",
            )
        )
        assert "test-host" in str(settings.REDIS_URL)
