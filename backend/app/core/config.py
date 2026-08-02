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

    # Tokens a brand-new account starts with (granted on registration — see
    # UserManager.on_after_register), so signing up is enough to publish a first
    # post without having to review anything first. Also folded into
    # `rebuild_from_pg`'s token seeding (starting balance + reviewed_count), so a
    # Redis rebuild doesn't retroactively strip a never-reviewed account's grant.
    FEED_STARTING_TOKENS: int = 5

    # --- Redis-backed feed distribution algorithm ---
    # Per-user review-queue capacity. A user is in the `free_queue` set while their
    # queue holds fewer than this many post_ids.
    FEED_QUEUE_MAX_SLOTS: int = 20
    # Recipients (K) each operation fans a post out to.
    FEED_FANOUT: int = 3
    # Recipient selection samples K * this many channel subscribers, then keeps the
    # ones with a free slot (see service.select_recipients). Higher ⇒ more reliably
    # finds K free recipients in a saturated channel, at the cost of a larger (still
    # O(sample)) membership check. Must be >= 1.
    FEED_FANOUT_SAMPLE_MULTIPLIER: int = 4
    # Dynamic admission price for creating an original post, as a function of the
    # operation-queue length: clamp(MIN + len(ops) // STEP_ITEMS, MIN, MAX).
    FEED_PRICE_MIN: int = 1
    FEED_PRICE_MAX: int = 5
    FEED_PRICE_STEP_ITEMS: int = 20
    # The price above is expensive to keep consistent if computed live on every
    # request (two calls a few seconds apart can see different queue lengths). Instead
    # a background task recomputes it on a timer and publishes one shared snapshot that
    # every reader and every charge reads from (see app/feed/service.py:
    # refresh_price_snapshot / get_price_snapshot). The snapshot's `expires_at` —
    # computed_at + FEED_PRICE_REFRESH_SECONDS — is a guarantee shown to clients (see
    # GET /posts/economy) that the price will not change before then; the refresh loop
    # honors it even across several uncoordinated processes (see run_price_refresher).
    # FEED_PRICE_TTL_SECONDS is unrelated to that guarantee — it's just the Redis key's
    # own TTL, comfortably longer than the refresh interval, so a stalled refresher
    # shows up as a missing snapshot rather than a silently stale price served forever.
    FEED_PRICE_REFRESH_SECONDS: int = 60
    FEED_PRICE_TTL_SECONDS: int = 90
    # Seconds an undeliverable operation (no free recipient) waits before retry.
    FEED_RETRY_INTERVAL_SECONDS: int = 20
    # How long an operation may keep retrying before it is abandoned (5 days). Without
    # this a post published to a channel that never gains a free subscriber would cycle
    # through the stream forever, and its presence in XLEN would inflate the admission
    # price for everyone. The deadline is set on the first park and carried across
    # re-parks, so it bounds total age, not the gap between attempts.
    FEED_RETRY_MAX_AGE_SECONDS: int = 5 * 24 * 60 * 60

    # --- delivery exclusions ---
    # Never fan a post out to its own author. Free to enforce: `author_id` rides along
    # on the stream entry, so filtering it costs no extra round trip and no stored state.
    # Note the side effect — a channel whose only free subscriber is the author now
    # delivers nothing, and the op is abandoned rather than retried (see
    # service.has_eligible_recipient).
    FEED_EXCLUDE_OWN_POSTS: bool = True
    # Never deliver a post to a user it has already reached. Backed by a per-post
    # `seen:{post_id}` set written *atomically by the `place` script*, so a user is
    # recorded the instant the post lands in their queue — before they could possibly
    # review or forward it, which is what makes it race-free. Postgres' unique
    # (user, post) review constraint stays the backstop, so losing the set degrades to
    # today's 409 rather than breaking correctness.
    FEED_EXCLUDE_SEEN: bool = True
    # Lifetime of a `seen:{post_id}` set, refreshed on every delivery. Must exceed
    # FEED_RETRY_MAX_AGE_SECONDS, or a post still circulating could outlive the record
    # of who has already had it.
    FEED_SEEN_TTL_SECONDS: int = 7 * 24 * 60 * 60

    # --- operation stream (Redis Streams consumer group) ---
    # How long (ms) a delivered-but-unacked op may sit idle before another consumer
    # may reclaim it (XAUTOCLAIM). Must exceed the worst-case fan-out time for one op,
    # or a slow op gets reclaimed and processed twice (tolerated, but wasteful).
    FEED_STREAM_CLAIM_MIN_IDLE_MS: int = 30_000
    # Max abandoned ops a single reclaim sweep pulls back per loop iteration.
    FEED_STREAM_RECLAIM_COUNT: int = 10
    # Block up to this long (seconds) waiting for a new stream entry per read.
    FEED_STREAM_BLOCK_SECONDS: float = 1.0

    # --- interaction rate limiting ---
    # Feed writes (create a post, forward, drop) share one per-user budget: at most
    # INTERACTION_RATE_LIMIT of them in any sliding window of
    # INTERACTION_RATE_WINDOW_SECONDS (see app/core/rate_limit.py). One shared budget
    # rather than one per endpoint, so alternating between them doesn't dodge it.
    # Set the limit to 0 to disable rate limiting entirely.
    INTERACTION_RATE_LIMIT: int = 5
    INTERACTION_RATE_WINDOW_SECONDS: float = 10.0

    # --- localization ---
    # Locales the API can resolve `Accept-Language` into (app.core.locale), and the
    # set of locales the banner accepts a message for (app.schemas.banner). ISO
    # 639-1 codes. "en" must always be present - it's the fallback used when a
    # request's locale is missing/unsupported and when a banner has no text for the
    # resolved locale. Extending this list (plus a matching translation in the
    # Flutter app's `lib/l10n/*.arb` and `supportedAppLocales`) is the whole story
    # for adding a language - no other backend code changes.
    SUPPORTED_LOCALES: list[str] = ["en", "de"]
    DEFAULT_LOCALE: str = "en"

    # --- password policy ---
    # Off: fastapi-users applies no length/composition rule at all (see
    # UserManager.validate_password) - "the user can enter any password he wants",
    # by explicit design. On: at least PASSWORD_MIN_LENGTH characters, spanning at
    # least PASSWORD_MIN_CHARACTER_CLASSES of {lowercase, uppercase, digit, symbol}.
    REQUIRE_STRONG_PASSWORD: bool = False
    PASSWORD_MIN_LENGTH: int = 10
    PASSWORD_MIN_CHARACTER_CLASSES: int = 3

    # --- email verification ---
    # Off: `is_verified` is never checked (see app.deps.users.CurrentVerifiedUser) and
    # no code is ever sent on registration - an unverified account works exactly
    # like a verified one. On: CurrentVerifiedUser rejects unverified, non-superuser
    # accounts with 403 "unverified_user" everywhere it's used (feed/channels/items/
    # stats), and registration sends a short numeric code by email that the client
    # must redeem via POST /auth/email-verification/confirm before those routes work.
    # `GET/PATCH /users/me` deliberately stay open to unverified users regardless -
    # the client needs the former to see `is_verified` at all, and the latter is
    # self-service account editing rather than a platform action.
    REQUIRE_EMAIL_VERIFICATION: bool = True
    EMAIL_VERIFICATION_CODE_LENGTH: int = 6
    EMAIL_VERIFICATION_CODE_TTL_SECONDS: int = 15 * 60
    # A submitted-but-wrong code counts even if it's for a code that has already
    # since expired, so this also bounds brute-forcing an old code's Redis key.
    EMAIL_VERIFICATION_MAX_ATTEMPTS: int = 5
    EMAIL_VERIFICATION_RESEND_COOLDOWN_SECONDS: int = 60

    # --- outbound email (SMTP) ---
    # Any relay works unchanged (Gmail SMTP, AWS SES, Mailgun, Postmark, ...) - just
    # set these in .env. Left unset (the default, e.g. local dev/tests), send_email
    # logs the message instead of sending it, so registering an account never
    # requires real SMTP credentials to work end-to-end.
    SMTP_HOST: str | None = None
    SMTP_PORT: int = 587
    SMTP_USERNAME: str | None = None
    SMTP_PASSWORD: str | None = None
    SMTP_USE_TLS: bool = True
    SMTP_FROM_EMAIL: str = "no-reply@poulsekora.app"
    SMTP_FROM_NAME: str = "Poulse Kora"

    # --- supporter subscription (payments) ---
    # Master switch: off means every /subscriptions/* route answers 404
    # "subscriptions_disabled" and no Mollie call is ever made, so the feature can be
    # merged and deployed dark, then flipped on later with no redeploy. The webhook
    # route is the one exception — it keeps processing regardless, so a subscription
    # already in flight when this gets toggled off isn't stranded mid-payment.
    SUBSCRIPTIONS_ENABLED: bool = False
    # Mollie (https://www.mollie.com) processes the actual payment; see
    # app/core/mollie.py and app/api/subscriptions.py for the full flow. Test-mode
    # keys (`test_...`) work against the same API for local dev.
    MOLLIE_API_KEY: str | None = None
    MOLLIE_API_BASE_URL: str = "https://api.mollie.com/v2"
    # Price and billing interval of the "supporter" plan. Amount is Mollie's expected
    # format: a decimal string with exactly 2 places, e.g. "5.00".
    SUPPORTER_PRICE_AMOUNT: str = "1.99"
    SUPPORTER_PRICE_CURRENCY: str = "EUR"
    # Mollie Subscriptions API interval format, e.g. "1 month", "1 year".
    SUPPORTER_INTERVAL: str = "1 month"
    # This backend's own public HTTPS origin, used to build the webhook callback URL
    # passed to Mollie (must be reachable from the internet — a local dev server
    # needs a tunnel, e.g. ngrok, for webhooks to ever arrive).
    PUBLIC_BASE_URL: str = "http://localhost:8000"
    # Where Mollie's hosted checkout redirects the browser after payment. For the MVP
    # (web checkout only, no native app purchase yet — see CLAUDE.md) this is a plain
    # web page; once the app opens checkout itself, point this at a custom URL scheme
    # / universal link the app can catch instead.
    SUBSCRIPTION_CHECKOUT_REDIRECT_URL: str = "http://localhost:8000/"

    BACKEND_CORS_ORIGINS: list[str] = []

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
