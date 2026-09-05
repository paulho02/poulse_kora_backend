import sys
from functools import cached_property
from typing import Any

from pydantic import Field, HttpUrl, PostgresDsn, RedisDsn, field_validator
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
    FEED_STARTING_TOKENS: int = 10

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
    # Dynamic admission price for creating an original post. Rather than a fixed
    # queue-length threshold (a given backlog means something very different at 5
    # active users vs 100,000), the price is nudged by exactly 1 toward whichever
    # side of a *relative* target the fan-out queue currently sits on (see
    # app/feed/pricing.py: compute_price / price_target). A bigger step would
    # overshoot and hunt back and forth (a pendulum); nudging by 1 per refresh tick
    # trades responsiveness for stability on purpose.
    FEED_PRICE_MIN: int = 1
    FEED_PRICE_MAX: int = 30
    # Target fan-out queue length, as a fraction of currently active users
    # (service.active_user_count) — the "healthy buffer" the price steers toward.
    FEED_PRICE_BUFFER_RATIO: float = 0.1
    # Floor for the target above, so a small/zero active-user count (cold start, a
    # quiet night) doesn't collapse the target to 0-1 and ratchet price up on noise.
    # `price_target` takes whichever of this or the ratio above is larger.
    FEED_PRICE_TARGET_MIN_ITEMS: int = 20
    # Band around the target, as a fraction of it, within which the price is left
    # untouched rather than nudged — without this, a price sitting one item off
    # target would hunt between two adjacent values forever (+1, -1, +1, ...) even
    # though it's already about as close as a single-item queue metric can get.
    FEED_PRICE_DEADBAND_RATIO: float = 0.1
    # How far a single channel's price may stray from the global one, as a fraction
    # (0.25 ⇒ 75%-125% of the base price). Each channel is priced by nudging the shared
    # global price, never by running its own controller — see app/feed/pricing.py:
    # channel_factor. The band is the entire stability mechanism: the factor is
    # stateless, so unlike the global price it cannot wander, it can only be clamped.
    FEED_PRICE_CHANNEL_BAND: float = 0.25
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
    # Sliding window for "currently active" (see service.mark_active /
    # active_user_count): a user counts as active until this many seconds pass without
    # another feed fetch. 60s means a refresh at 59s keeps them active; the window
    # resets on every fetch rather than counting from first-seen.
    ACTIVE_USER_WINDOW_SECONDS: int = 60
    # Seconds an undeliverable operation (no free recipient) waits before retry.
    FEED_RETRY_INTERVAL_SECONDS: int = 20
    # How long an operation may keep retrying before it is abandoned (10 days). Without
    # this a post published to a channel that never gains a free subscriber would cycle
    # through the stream forever, and its presence in XLEN would inflate the admission
    # price for everyone. The deadline is set on the first park and carried across
    # re-parks, so it bounds total age, not the gap between attempts.
    #
    # This is the one knob that decides how long a post keeps *looking* for an audience,
    # and it is deliberately generous: a post is only ever parked because nobody had a
    # free slot, so abandoning one early throws away reach its author paid for. A post
    # that has genuinely run out of audience never waits this out — `has_eligible_-
    # recipient` abandons it on the spot — so the deadline only ever bites the case it
    # is meant to: a channel too quiet to drain its subscribers' queues within it.
    FEED_RETRY_MAX_AGE_SECONDS: int = 10 * 24 * 60 * 60

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
    # How much longer than the retry deadline a `seen:{post_id}` set must survive, as a
    # multiple of it. See FEED_SEEN_TTL_SECONDS below for what the extra window buys.
    FEED_SEEN_TTL_RETRY_MULTIPLE: int = Field(default=2, ge=2)

    @property
    def FEED_SEEN_TTL_SECONDS(self) -> int:
        """Lifetime of a `seen:{post_id}` set, refreshed on every delivery.

        Derived rather than configured, because there is no such thing as a sensible
        value for it that is not a function of FEED_RETRY_MAX_AGE_SECONDS — and the
        one way to get the pair wrong fails *silently*: a set that expires while an op
        is still parked leaves the next fan-out with no record of who has had the post,
        and the only symptom is someone being handed a post they already reviewed.
        Deriving it means that pairing cannot be expressed, which beats validating it.

        Why a multiple and not just "a bit more". The set's clock runs from the post's
        last *delivery*; a parked op's deadline runs from its *park*. Those are not the
        same instant, so the set has to cover both the retry window and however long the
        post sat undelivered before that op existed — a forward, say, from someone who
        left the post unread in their queue for a while. Allowing one retry window for
        each is the reasoning behind the default of 2; there is no exact answer here,
        because dwell time has no hard bound, which is also why Postgres' unique
        (user, post) review constraint stays the real backstop rather than this.

        The cost of being generous is Redis memory — a set of recipient ids per post,
        held longer — which is small next to the media those posts carry.
        """
        return self.FEED_RETRY_MAX_AGE_SECONDS * self.FEED_SEEN_TTL_RETRY_MULTIPLE

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

    # --- change-password rate limiting ---
    # At most PASSWORD_CHANGE_RATE_LIMIT attempts per user in any sliding window of
    # PASSWORD_CHANGE_RATE_WINDOW_SECONDS (see app/core/rate_limit.py) - the current
    # password is a guessable secret being checked here, so this route needs its own
    # budget rather than sharing INTERACTION_RATE_LIMIT. Set the limit to 0 to
    # disable rate limiting entirely.
    PASSWORD_CHANGE_RATE_LIMIT: int = 5
    PASSWORD_CHANGE_RATE_WINDOW_SECONDS: float = 300.0

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

    # --- Google sign-in ---
    # Off: POST /auth/google and /auth/google/link answer 400 "google_oauth_disabled"
    # and GET /config reports the feature off, so the client hides the button - the
    # feature ships dark and is flipped on once the Google Cloud OAuth clients exist.
    GOOGLE_OAUTH_ENABLED: bool = False
    # Accepted `aud` values on incoming ID tokens. The mobile app initializes
    # google_sign_in with `serverClientId` = the *web* client ID, so Android and web
    # both mint tokens audienced to that same single value; this is a list only so an
    # iOS client ID can be added later without a code change. Verified explicitly in
    # app/core/google_oauth.py - an empty list accepts nothing.
    GOOGLE_CLIENT_IDS: list[str] = []

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

    # --- object storage (S3-compatible) ---
    # Where every uploaded image, video and poster frame lives. One protocol, two
    # deployments: a MinIO container locally/in CI, a Railway Bucket (Tigris) in
    # production - see app/core/storage.py and RAILWAY.md.
    STORAGE_ENDPOINT_URL: str = "http://minio:9000"
    # The endpoint *clients* connect to, when it differs from the one the backend
    # uses. It usually does in local dev: the backend reaches MinIO as `minio:9000`
    # on the compose network, while a phone or browser has to reach it as
    # `localhost:9000` (or `10.0.2.2:9000` from the Android emulator). This is not
    # cosmetic - the host is a *signed* header, so a URL signed against one name and
    # rewritten to another is rejected. Unset means "same as STORAGE_ENDPOINT_URL",
    # which is the correct setting on Railway.
    TEST_STORAGE_PUBLIC_ENDPOINT_URL: str | None = None
    STORAGE_PUBLIC_ENDPOINT_URL: str | None = None

    @field_validator("STORAGE_PUBLIC_ENDPOINT_URL", mode="before")
    @classmethod
    def build_test_storage_public_endpoint(
        cls, v: str | None, info: dict[str, Any]
    ) -> str | None:
        """Under pytest the "client" fetching a presigned URL is the test process,
        which runs *inside* the backend container - so it reaches MinIO as
        `minio:9000`, not as the `localhost:9000` a phone or browser would use.
        Without this swap every media assertion would fail on a connection error
        that has nothing to do with the code under test.
        """
        if "pytest" in sys.modules:
            return info.data.get("TEST_STORAGE_PUBLIC_ENDPOINT_URL") or v
        return v
    # Declared before STORAGE_BUCKET so the validator below can see it: pydantic
    # fills `info.data` in field-definition order.
    TEST_STORAGE_BUCKET: str | None = None
    STORAGE_BUCKET: str = "poulse-kora-media"

    @field_validator("STORAGE_BUCKET", mode="before")
    @classmethod
    def build_test_storage_bucket(cls, v: str | None, info: dict[str, Any]) -> str:
        """Swaps in TEST_STORAGE_BUCKET while pytest is running, mirroring what
        DATABASE_URL/REDIS_URL already do - so a test run can never write objects
        into (or delete objects out of) the bucket the dev app is using.
        """
        if "pytest" in sys.modules:
            test_bucket = info.data.get("TEST_STORAGE_BUCKET")
            if not test_bucket:
                raise ValueError(
                    "pytest detected, but TEST_STORAGE_BUCKET is not set in environment"
                )
            return str(test_bucket)
        return v

    STORAGE_REGION: str = "us-east-1"  # Railway/Tigris wants "auto"
    STORAGE_ACCESS_KEY_ID: str = ""
    STORAGE_SECRET_ACCESS_KEY: str = ""
    # "path" -> http://host/<bucket>/<key>, "virtual" -> https://<bucket>.host/<key>.
    # MinIO on localhost can only do path-style (there is no wildcard DNS for
    # `<bucket>.localhost`); Railway/Tigris serves virtual-hosted URLs.
    STORAGE_ADDRESSING_STYLE: str = "path"
    # Local dev and CI only: create the bucket on boot (and before a migration that
    # needs it) if it is missing. On Railway the platform provisions the bucket and
    # the credentials are scoped to it, so this stays off there.
    STORAGE_AUTO_CREATE_BUCKET: bool = False

    # --- presigned media URLs ---
    # How long a handed-out media URL stays valid. This is the window in which a URL
    # that leaked (forwarded screenshot, shared link) still works, so it is a real
    # security parameter, not just a cache knob - the authorization check now happens
    # once, when the URL is minted, not on every byte fetch.
    MEDIA_URL_TTL_SECONDS: int = 60 * 60  # 1 hour
    # How often the *string* changes. Signatures are quantized to this boundary so
    # the same object yields a byte-identical URL within the window, which is what
    # lets the client cache media by URL instead of re-downloading the whole feed's
    # images on every refresh (see app/core/storage.py). Must be comfortably smaller
    # than the TTL: a URL is only guaranteed `TTL - REFRESH` of life when handed out.
    MEDIA_URL_REFRESH_SECONDS: int = 15 * 60

    # --- profile pictures ---
    # Stored in the bucket above, keyed by `User.profile_picture_key`. Displayed
    # beside a post's author (app/api/posts.py: _serialize_post), never for an
    # anonymous post.
    # Bounds the *upload*; the stored image is re-encoded and downscaled to
    # PROFILE_PICTURE_MAX_DIMENSION_PX (app/core/media_validation.py:
    # process_profile_picture), so it is normally far smaller than this.
    PROFILE_PICTURE_MAX_BYTES: int = 2 * 1024 * 1024  # 2 MB
    # Longest side after re-encode. The client's cropper already hands back a
    # 512px square, so this is a ceiling on anything that reaches the route by
    # another path rather than a resize the app normally triggers.
    PROFILE_PICTURE_MAX_DIMENSION_PX: int = 512
    PROFILE_PICTURE_ALLOWED_CONTENT_TYPES: list[str] = [
        "image/jpeg",
        "image/png",
        "image/webp",
    ]

    # --- post media (images & videos) ---
    # Stored in the bucket (see PostMedia in app/models/post_media.py); the database
    # keeps only the object key and the metadata a feed needs to lay the block out.
    # Images are re-encoded server-side (EXIF/GPS stripped, downscaled to
    # POST_IMAGE_MAX_DIMENSION_PX) via app/core/media_validation.py, so
    # POST_IMAGE_MAX_BYTES bounds the *upload*, not the stored size. Video is fully
    # transcoded via ffmpeg/ffprobe in the same module - POST_VIDEO_MAX_BYTES bounds
    # both the upload and worst-case per-request memory, since validation holds the
    # whole clip in memory before it is streamed to the bucket. Byte *serving* no
    # longer costs this process anything: clients fetch objects directly, and range
    # requests (which is how a video player scrubs) are the bucket's problem.
    POST_MEDIA_MAX_FILES: int = 5
    POST_MEDIA_MAX_TOTAL_BYTES: int = 40 * 1024 * 1024  # 40 MB combined per post
    POST_IMAGE_MAX_BYTES: int = 12 * 1024 * 1024  # upload cap, pre-re-encode
    POST_IMAGE_MAX_DIMENSION_PX: int = 2048  # longest side after re-encode
    POST_IMAGE_ALLOWED_CONTENT_TYPES: list[str] = [
        "image/jpeg",
        "image/png",
        "image/webp",
    ]
    POST_VIDEO_MAX_BYTES: int = 25 * 1024 * 1024  # per-file upload cap
    POST_VIDEO_MAX_DURATION_SECONDS: int = 60  # via ffprobe, never client-trusted
    POST_VIDEO_ALLOWED_CONTENT_TYPES: list[str] = ["video/mp4", "video/quicktime"]
    # Video is re-encoded to H.264/AAC on upload rather than stored as-is - see
    # media_validation.py's _transcode_video for why (phones default to HEVC,
    # which no Chromium-based browser can decode, so an as-uploaded clip is
    # simply unplayable on web). These bound the *output*: the longest edge is
    # scaled down to fit POST_VIDEO_MAX_DIMENSION_PX and the bitrate is capped,
    # which keeps stored objects (and the bandwidth every viewer pays to fetch
    # one) a sane size - a 6s 1080p phone clip arrives at ~15 MB and leaves at
    # ~1-2 MB.
    POST_VIDEO_MAX_DIMENSION_PX: int = 1280
    POST_VIDEO_TARGET_CRF: int = 26
    POST_VIDEO_MAX_BITRATE: str = "4M"
    # --- fixed aspect ratios ---
    # Every attachment ends up at exactly one of two shapes. Consumers scroll a
    # single-column feed, so free-form ratios meant every post resized the column
    # differently and a client could not reserve space before the bytes arrived;
    # two known shapes make the layout predictable and let the feed size a media
    # block from `PostMediaRead.width/height` alone.
    #
    # The two paths reach that differently, and deliberately so: an **image** is
    # cropped by the user in the client (which owns the only UI that can show them
    # what they are losing) and merely *validated* here, while a **video** cannot be
    # re-encoded in a Flutter client at all, so the client sends only an
    # orientation and the center crop is applied here - free, inside the full
    # transcode _transcode_video already runs.
    POST_MEDIA_LANDSCAPE_RATIO: float = 4 / 3
    POST_MEDIA_PORTRAIT_RATIO: float = 4 / 5
    # Rounding slack for the image check. A client crops to a whole-pixel box, so
    # e.g. 1440x1080 is exact but 1439x1080 is not - 2% absorbs that without
    # admitting a visibly different shape (4:3 vs 5:4 differ by ~7%).
    POST_MEDIA_RATIO_TOLERANCE: float = 0.02
    # Longest side of the still frame stored beside every video
    # (PostMedia.poster_object_key).
    # It is a placeholder shown until playback starts, never a full-size image, so
    # it is kept small - it is fetched by every feed card that has a video on it.
    POST_VIDEO_POSTER_MAX_DIMENSION_PX: int = 720
    POST_VIDEO_POSTER_QUALITY: int = 6  # ffmpeg -q:v, 2 (best) .. 31 (worst)

    # Sanity cap on total blocks per post (text + media combined, see PostBlock) -
    # guards against a pathological submission (thousands of tiny blocks), not a
    # real authoring limit.
    POST_BLOCKS_MAX_COUNT: int = 40

    # --- feedback ---
    # User-submitted feedback / bug reports (app/models/feedback.py). Attachments go
    # through the same validation module as post media but on their own path
    # (`process_feedback_upload`), because the two fixed post ratios must NOT apply
    # here: a screenshot is whatever shape the reporter's screen is, and cropping a
    # screen recording to 4:5 would throw away the part being reported.
    FEEDBACK_MESSAGE_MAX_LENGTH: int = 4000
    FEEDBACK_MEDIA_MAX_FILES: int = 5
    FEEDBACK_MEDIA_MAX_TOTAL_BYTES: int = 40 * 1024 * 1024
    FEEDBACK_IMAGE_MAX_BYTES: int = 12 * 1024 * 1024  # upload cap, pre-re-encode
    FEEDBACK_IMAGE_MAX_DIMENSION_PX: int = 2048  # longest side after re-encode
    FEEDBACK_VIDEO_MAX_BYTES: int = 25 * 1024 * 1024
    # More generous than POST_VIDEO_MAX_DURATION_SECONDS: reproducing a bug on
    # camera takes longer than a post is meant to be.
    FEEDBACK_VIDEO_MAX_DURATION_SECONDS: int = 120
    # Deliberately reuses the POST_* allow-lists rather than defining its own: what
    # may be uploaded here is a fact about what Pillow/ffmpeg can decode, which is
    # the same question in both places. The *ratio* rule is what differs, and that
    # lives in process_feedback_upload, not in a content-type list.
    #
    # Feedback is submittable while signed out (it is reachable from the login
    # screen), so this budget is keyed by user id when there is one and by client IP
    # otherwise - see app/deps/rate_limit.py: limit_feedback. Set to 0 to disable.
    FEEDBACK_RATE_LIMIT: int = 5
    FEEDBACK_RATE_WINDOW_SECONDS: float = 3600.0

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
