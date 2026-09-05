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


class TestSeenTtl:
    """The `seen:{post_id}` TTL is derived from the retry deadline, not configured.

    There is no sensible value for it that is not a function of
    FEED_RETRY_MAX_AGE_SECONDS: the set has to outlive every op still trying to deliver
    that post, and the one way to get the pair wrong fails silently — the set expires
    mid-retry, the next fan-out forgets who has had the post, and someone is handed one
    they already reviewed. Deriving it makes that pairing inexpressible; these tests
    pin that it stays derived.
    """

    @staticmethod
    def _kwargs(**overrides):
        return _base_kwargs(
            TEST_DATABASE_URL="postgresql://test-host/apptest",
            TEST_REDIS_URL="redis://test-host:6379/1",
            **overrides,
        )

    def test_tracks_the_retry_deadline(self):
        settings = Settings(**self._kwargs(FEED_RETRY_MAX_AGE_SECONDS=1000))
        assert settings.FEED_SEEN_TTL_SECONDS == 2000

    def test_always_outlives_the_retry_deadline(self):
        # The invariant the old validator used to police, now true by construction —
        # including for a retry deadline nobody anticipated.
        for retry_age in (1, 60, 5 * 24 * 60 * 60, 365 * 24 * 60 * 60):
            settings = Settings(**self._kwargs(FEED_RETRY_MAX_AGE_SECONDS=retry_age))
            assert settings.FEED_SEEN_TTL_SECONDS > retry_age

    def test_multiple_below_two_is_refused(self):
        # 1 would make the set expire exactly as the last retry falls due, which is the
        # same race with the margin removed; 0 would delete the key outright.
        for bad in (0, 1):
            with pytest.raises(ValidationError):
                Settings(**self._kwargs(FEED_SEEN_TTL_RETRY_MULTIPLE=bad))

    def test_setting_the_ttl_directly_is_refused(self):
        # Not silently ignored: `extra="forbid"` means a deployment still carrying the
        # old env var fails at startup with the name in the message, rather than
        # running on a value it believes it set.
        with pytest.raises(ValidationError, match="FEED_SEEN_TTL_SECONDS"):
            Settings(**self._kwargs(FEED_SEEN_TTL_SECONDS=7 * 24 * 60 * 60))
