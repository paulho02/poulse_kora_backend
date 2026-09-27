import sys
from functools import cached_property
from typing import Any, Literal

from pydantic import Field, HttpUrl, PostgresDsn, RedisDsn, field_validator

from app.core.username_policy import USERNAME_PATTERN
from pydantic.networks import AnyHttpUrl
from pydantic_settings import BaseSettings

# The language value carried by a post that has none - a photo, a video, a
# caption-free meme. ISO 639-2's "undetermined" code, chosen so it can never
# collide with a real ISO 639-1 entry in CONTENT_LANGUAGES.
#
# A module constant rather than a setting, and it lives here rather than in
# app/core/languages.py only to keep that module free to import `settings`:
# every branch that treats this value specially (routing through the whole
# channel instead of one language's slice of it, being the one value a text-free
# post may carry) is code, not configuration, so a deployment that changed the
# string could only break things.
LANGUAGE_UNSPECIFIED = "und"


class Settings(BaseSettings):
    PROJECT_NAME: str = "Peerkola Backend"

    SENTRY_DSN: HttpUrl | None = None

    API_PATH: str = "/api/v1"

    # --- logging ---
    # See app/core/logger.py for the whole strategy; these are the knobs it reads.
    #
    # ENVIRONMENT is stamped on every log line and is otherwise unused - it is
    # the field you filter on when staging and production ship logs to the same
    # place, so it is worth setting per Railway environment ("production",
    # "int", ...).
    ENVIRONMENT: str = "development"
    LOG_LEVEL: str = "INFO"
    # "auto" -> json when running on Railway, console otherwise. Railway parses
    # JSON on stdout into filterable attributes, which is the entire reason for
    # the format; a terminal wants the other one. Override to pin either.
    LOG_FORMAT: str = "auto"
    # Per-logger levels, e.g. '{"app.feed": "DEBUG"}' to watch fan-out on a live
    # deploy without turning the whole process up. Applied after the built-in
    # library levels, so it can also un-mute a noisy dependency.
    LOG_LEVEL_OVERRIDES: dict[str, str] = {}
    # One line per request from our own middleware (app/core/request_logging.py),
    # which knows the duration, the request id, the resolved route and the user.
    # Turning it off hands the job back to uvicorn's plainer access log rather
    # than leaving none at all.
    LOG_ACCESS: bool = True
    # Requests to these paths are logged at DEBUG instead of INFO. Both defaults
    # are polled on a timer rather than by a person - Railway's health probe and
    # the client's reconnect loop hit the first, the feed screen polls the second
    # while it is open - so at any real user count they would be the overwhelming
    # majority of an INFO log and none of its content.
    LOG_QUIET_PATHS: list[str] = ["/api/v1/health", "/api/v1/posts/feed/status"]
    # Requests at or above this are logged at WARNING whatever their status, so a
    # route that has started crawling surfaces on its own.
    LOG_SLOW_REQUEST_MS: int = 1500
    # Emit every SQL statement SQLAlchemy runs. Useful for a few minutes when
    # chasing a slow query, unusable as a standing setting.
    LOG_SQL: bool = False
    # An IP address is personal data under GDPR, and it is also the only thing
    # that makes abuse from signed-out traffic investigable. On by default; this
    # is the one flag that removes it from every line.
    #
    # There is deliberately no companion setting for *where* to read the address
    # from (a socket peer behind Railway's proxy is a useless constant; an
    # X-Forwarded-For header with no proxy in front is a lie). That is a fact
    # about where the process runs, not a preference, so it is derived - see
    # app/core/request_logging.py: _client_ip.
    LOG_CLIENT_IP: bool = True

    ACCESS_TOKEN_EXPIRE_MINUTES: int = 7 * 24 * 60  # 7 days

    # Number of posts a user must review (forward or drop) before they can create one.
    # Superusers still bypass gating; no longer gates posting (replaced by the token
    # economy, see the FEED_* settings below), but kept for reference/compatibility.
    REVIEW_GATE: int = 5

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
    # How far a single route's price may stray from the global one, as a fraction
    # (0.5 ⇒ 50%-150% of the base price). Each route is priced by nudging the shared
    # global price, never by running its own controller — see app/feed/pricing.py:
    # route_factor. The band is the entire stability mechanism: the factor is
    # stateless, so unlike the global price it cannot wander, it can only be clamped.
    #
    # Widened from 0.25 when language routing landed. At ±25% the whole spread across
    # every route in the deployment collapsed to one or two tokens once rounded to
    # integers — correct arithmetic, but it made the range the client shows
    # uninformative, and it under-reacted to routes that genuinely differ in capacity
    # (a language with twenty readers absorbing the same posting rate as one with five
    # thousand). At ±50% a base price of 4 spans 2-6, which a reader can act on.
    #
    # It is a real economy lever, not a display setting: a congested route now costs
    # half again as much as the shared price and a quiet one half as much, so raising
    # it further trades price stability for responsiveness.
    FEED_PRICE_CHANNEL_BAND: float = 0.5
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
    #
    # Sized against a reader working through their queue, not against an abuser:
    # a forward or a drop is one tap, the card takes ~1.1s to play its score and
    # slide away, and a reader who already knows what they think of the next post
    # taps straight through that. At 5 per 10s that reader hit the limit in
    # ordinary use — the throttle was landing on the behaviour the feed is built
    # to encourage. Doubled to 10, which still bounds a scripted client to a rate
    # no thumb reaches.
    INTERACTION_RATE_LIMIT: int = 13
    INTERACTION_RATE_WINDOW_SECONDS: float = 10.0

    # --- authentication rate limiting ---
    # Login is the one route where an attacker's input is checked against a secret
    # they do not hold, and every attempt costs an argon2 verify (64 MiB, on the
    # event loop) whether the account exists or not - so an unthrottled login is
    # both a brute-force path and the cheapest DoS this process offers. Two budgets
    # are spent per attempt (see app/deps/rate_limit.py: limit_login): one keyed on
    # the caller's address, one on the lowercased email being tried. The address
    # budget bounds the CPU any one host can burn; the account budget bounds the
    # guesses one account can receive from *everywhere*, which a per-IP limit alone
    # cannot (a botnet gets a fresh allowance per host). Set either limit to 0 to
    # disable that half.
    LOGIN_RATE_LIMIT_PER_IP: int = 20
    LOGIN_RATE_LIMIT_PER_ACCOUNT: int = 10
    LOGIN_RATE_WINDOW_SECONDS: float = 300.0
    # Registration hashes a password *and* sends a verification mail to an address
    # the caller chose, so left open it is a mail cannon with our sending domain's
    # reputation as the ammunition. Keyed on the caller's address only - there is no
    # account yet. Set to 0 to disable.
    REGISTER_RATE_LIMIT: int = 5
    REGISTER_RATE_WINDOW_SECONDS: float = 3600.0
    # Changing `email` re-sends a verification code to the *new* address on every
    # write (see UserManager.on_after_update), which is the same mail cannon behind
    # a login. Keyed on the account whose address is being changed, enforced in
    # UserManager._update because fastapi-users' own router serves the PATCH. Set
    # to 0 to disable.
    EMAIL_CHANGE_RATE_LIMIT: int = 3
    EMAIL_CHANGE_RATE_WINDOW_SECONDS: float = 3600.0

    # --- request body size ---
    # Enforced by app/core/body_limit.py *before* a body is read: Starlette spools
    # multipart file parts to disk and FastAPI reads a JSON body whole, both before
    # any dependency (auth, a rate limit) runs, so without this a signed-out caller
    # can fill /tmp or RAM with one request. Two caps by content type, because a
    # multipart body is the only way anything large legitimately arrives: the
    # upload cap has to hold POST_MEDIA_MAX_TOTAL_BYTES / FEEDBACK_MEDIA_MAX_TOTAL_BYTES
    # plus the blocks JSON and multipart framing, the other cap only ever has to
    # hold a form or a JSON document. The caps bound what is *received*; the
    # per-file limits below still decide what is *kept*.
    MAX_REQUEST_BODY_BYTES: int = 64 * 1024
    MAX_UPLOAD_BODY_BYTES: int = 64 * 1024 * 1024

    # --- reviewer trust ---
    # How far a *forward* travels is scaled by the forwarder's Reviewer Trust score
    # (see app/core/trust.py for the formula and app/core/probes.py for the test
    # posts that feed it). Creator trust - a posting discount - is a separate feature
    # and none of these settings touch it.
    #
    # Turning this off leaves every forward at FEED_FANOUT and stops minting test
    # posts; scores are still computed and displayed, so the switch is about the
    # economy, not the UI.
    TRUST_ENABLED: bool = True
    # Everything the score reads is measured over this trailing window. Nothing is
    # lifetime, which is the whole reason an account cannot bank trust and coast on
    # it - and why an absent user returns at neutral rather than keeping either a
    # good or a bad record. There is no separate inactivity rule because there is
    # nothing for one to do.
    TRUST_WINDOW_DAYS: int = 30

    # Weights of the two *additive* components. Test posts dominate because they are
    # the only ground truth about whether someone actually read: everything else is
    # a proxy that a machine can imitate. Anomaly is deliberately not in this sum -
    # see TRUST_ANOMALY_* below.
    TRUST_WEIGHT_PROBE: float = 0.7
    TRUST_WEIGHT_VOLUME: float = 0.3

    # --- test posts (probes) ---
    # Probability that a reader's next queued post is a test rather than a real one,
    # rolled after each real review. 0 disables minting without disabling scoring.
    TRUST_PROBE_RATE: float = 0.05
    # Reviews that must pass between two probes. Without it the roll above can land
    # twice in a row, which is both annoying and the fastest way to teach someone
    # what a probe looks like.
    TRUST_PROBE_MIN_GAP_REVIEWS: int = 5
    # Probes needed before the probe component is trusted at full weight; below it
    # the component is shrunk toward neutral in proportion. Sized against
    # TRUST_PROBE_RATE: at 5% this is roughly 120 reviews, about a month of daily
    # reading. Low enough that an engaged reader reaches full confidence, high
    # enough that one unlucky mis-tap cannot decide a band.
    TRUST_PROBE_CONFIDENCE_N: int = 6
    # What a reader who never reads scores by chance. Half the probe variants ask to
    # be forwarded and half to be dropped, so blind answering converges here - and
    # chance has to map to *zero credit*, not to neutral, or ignoring probes would be
    # as good as passing them.
    TRUST_PROBE_CHANCE_RATE: float = 0.5
    # How many recent variants are remembered per reader, so the same wording does
    # not recur. Kept in Redis, expiring with the trust window.
    TRUST_PROBE_RECENT_MEMORY: int = 6
    # Username/email of the account probes are published as. Created on demand.
    # A real, named author rather than an anonymous post: if every probe were
    # anonymous, "anonymous" would itself become the tell, and every genuinely
    # anonymous post would inherit the suspicion.
    #
    # The address is under `example.com`, which RFC 2606 reserves and IANA holds
    # permanently - so nobody can ever register it and mail to it goes nowhere. Not
    # a `.invalid` or `.local` address, which express the same intent more clearly
    # but are rejected outright by the email validator behind `UserRead.email`: this
    # account is a real row, and a superuser listing users has to be able to
    # serialize it.
    TRUST_PROBE_AUTHOR_USERNAME: str = "peerkola"
    TRUST_PROBE_AUTHOR_EMAIL: str = "probes@peerkola.example.com"

    @field_validator("TRUST_PROBE_AUTHOR_USERNAME")
    @classmethod
    def validate_probe_author_username(cls, value: str) -> str:
        """The probe author is inserted by migration and by `_ensure_probe_author`,
        neither of which goes through `UserManager`, so a name outside the username
        rule would only surface as the CHECK constraint refusing the insert - on the
        first probe, in production. Refused at startup instead."""
        if not USERNAME_PATTERN.fullmatch(value):
            raise ValueError(
                "TRUST_PROBE_AUTHOR_USERNAME must match [a-z0-9_]+ "
                "(app/core/username_policy.py)"
            )
        return value

    # --- volume component ---
    # Reviews in the window below which volume contributes exactly neutral, and the
    # count at which it contributes its full weight. Deliberately bonus-only: reading
    # a lot can lift the score, reading little never lowers it. A casual reader is
    # quiet, not untrustworthy, and there is no version of this feature where being
    # busy is what earns someone's forwards a wider audience.
    TRUST_VOLUME_NEUTRAL_REVIEWS: int = 20
    TRUST_VOLUME_FULL_REVIEWS: int = 150

    # --- anomaly component ---
    # Anomaly is a *ceiling* on the above-neutral half of the score, never a term in
    # the sum (see app/core/trust.py: compute_score). So it can withhold the high
    # band but can never by itself produce a low one - which is what makes "dropping
    # a run of bad posts is allowed" a structural property rather than a tuning
    # value. Only failed test posts can cost a reader reach.
    #
    # Reviews in the window before the signal is trusted at all, shrunk in proportion
    # below it.
    TRUST_ANOMALY_CONFIDENCE_N: int = 20
    # A review closer than this to the previous one was not a read. Not a rate limit
    # (INTERACTION_RATE_LIMIT is that, an order of magnitude faster); this is the
    # floor below which no human is making a judgement.
    TRUST_MIN_READ_SECONDS: float = 1.5
    # Share of reviews allowed under that floor before it counts against anyone.
    # Generous because real reading is bursty - an obvious duplicate, a post whose
    # first line settles it, a double-tap - and only a *sustained* rate means
    # something.
    TRUST_PACE_TOLERANCE: float = 0.3
    # How far a reader's forward rate may sit from the deployment's before it counts,
    # as a fraction of the room available on that side (so 1.0 means "never forwards"
    # or "always forwards", whatever the population rate happens to be).
    #
    # 0.7 is very forgiving on purpose: where the population forwards a quarter of
    # what it sees, nothing at all is penalised until a reader's drop rate passes
    # ~92%, and the penalty only reaches full at literally zero forwards in 30 days.
    # Normalising per side is also what makes the signal asymmetric in the right
    # direction - blind *forwarding* is caught much harder than blind dropping,
    # because it spends the system's reach rather than only the dropper's time.
    TRUST_SKEW_TOLERANCE: float = 0.7
    # Global reviews needed in the window before a deployment-wide forward rate means
    # anything. Below it the skew signal is skipped entirely rather than measured
    # against noise - which is the state a fresh deployment is in.
    TRUST_FORWARD_RATE_MIN_SAMPLE: int = 200

    # --- bands ---
    # Score below LOW_MAX is the low band, at or above HIGH_MIN the high band,
    # anything between is normal reach. The multipliers are applied to FEED_FANOUT
    # and rounded, so at the default fan-out of 3 the three bands deliver 2 / 3 / 4.
    #
    # The low band is reachable only by answering test posts wrongly - anomalous
    # behaviour bottoms out at exactly 50 by construction (see trust.compute_score),
    # and volume alone tops out at 65, below HIGH_MIN. So these two numbers set how
    # much *probe* evidence a band change takes: at 35, an active reader has to be
    # near chance on probes to lose reach, while a casual one (whose volume component
    # is sitting at neutral) has to be failing about a third of them. Raising LOW_MAX
    # makes demotion easier on thin evidence, which is the wrong direction to be
    # wrong in.
    TRUST_BAND_LOW_MAX: int = 35
    TRUST_BAND_HIGH_MIN: int = 70
    TRUST_REACH_LOW_MULTIPLIER: float = 2 / 3
    TRUST_REACH_HIGH_MULTIPLIER: float = 4 / 3

    # --- caching ---
    # The score is computed lazily and cached, rather than recomputed by a background
    # job: a periodic job costs work proportional to the number of *accounts*, while
    # this costs work proportional to *activity*, and only an active reader ever
    # needs a score. Two indexed aggregates behind this TTL is the cheaper of the
    # two at every deployment size.
    TRUST_CACHE_TTL_SECONDS: int = 600
    # The deployment-wide forward rate is one aggregate shared by every reader, so it
    # is cached far longer than an individual score - it barely moves, and it is the
    # only part of the computation that scans more than one user's rows.
    TRUST_FORWARD_RATE_TTL_SECONDS: int = 900

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

    # --- content languages ---
    # The languages a *post* can be written in, and so the language half of the
    # feed's routing key (see app/feed/keys.py: audience). A post declares exactly
    # one; a reader accepts a set of them (User.content_languages); fan-out delivers
    # a post only into the intersection.
    #
    # Deliberately its own setting rather than a reuse of SUPPORTED_LOCALES, which is
    # about the language the API answers a *request* in. The two are free to diverge:
    # a language people write posts in does not need a translated error catalogue,
    # and a locale we translate into does not have to become a content bucket the day
    # its .arb file lands. ISO 639-1 codes.
    #
    # Adding one is: this list, a stopword list in the client's detector, and a
    # `python -m scripts.dangerous.rebuild_redis` so the new language's audience sets
    # exist. Removing one is *not* symmetric - see app/core/languages.py.
    CONTENT_LANGUAGES: list[str] = ["en", "de"]

    @field_validator("CONTENT_LANGUAGES")
    @classmethod
    def validate_content_languages(cls, value: list[str]) -> list[str]:
        """Reject the three ways this list can be wrong in a way nothing else would
        catch until posts had already been routed by it.

        A *field* validator, like `require_token_for_lettermint` and for the same
        reason: pydantic quotes the validated input back in the error it raises, and a
        model validator's input is the whole settings dict â€” so a crash here would
        print SECRET_KEY into the log on its way out.
        """
        cleaned = [code.strip().lower() for code in value if code.strip()]
        if not cleaned:
            # Every post would have to be UNSPECIFIED, and every reader's accepted
            # set would be empty, so nothing could ever route anywhere.
            raise ValueError("CONTENT_LANGUAGES must not be empty")
        if len(set(cleaned)) != len(cleaned):
            # A duplicate would double-count in `subs:total` (one membership per
            # language per subscription), quietly skewing every channel's price.
            raise ValueError("CONTENT_LANGUAGES must not contain duplicates")
        if LANGUAGE_UNSPECIFIED in cleaned:
            # "und" is the *absence* of a language and routes through the whole
            # channel. As a real entry it would also be a language slice, and a post
            # declaring it would take one branch here and the other there.
            raise ValueError(
                f"CONTENT_LANGUAGES must not contain {LANGUAGE_UNSPECIFIED!r} "
                "(the reserved 'no language' value)"
            )
        return cleaned

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

    # --- account data export (GDPR Art. 15 / Art. 20) ---
    # `GET /users/me/export` reads every table this account touches and streams
    # every object it uploaded back out of the bucket, so one call can be the
    # most expensive request this service serves. Its own budget for that reason
    # alone - it has nothing to do with the interaction economy, and sharing one
    # would let a download exhaust somebody's ability to post.
    #
    # The window is a day and the limit is small, but not one: a download that
    # failed half way is the *normal* reason to ask again, and answering that
    # with "come back tomorrow" would be an obstacle to a right rather than
    # protection of a resource. Art. 12(5) permits refusing manifestly excessive
    # repetition; three a day is nowhere near that line. Set the limit to 0 to
    # disable, like every other budget here.
    ACCOUNT_EXPORT_RATE_LIMIT: int = 3
    ACCOUNT_EXPORT_RATE_WINDOW_SECONDS: float = 24 * 60 * 60

    # Where a data-subject request goes when the automated export is not enough -
    # printed in the export's README, and the address named in the privacy
    # policy. A setting rather than a literal because the export is the one place
    # the backend states it, and a project that changes its support address must
    # not have to change code to stop misdirecting legal requests.
    SUPPORT_EMAIL: str = "support@poulse.com"

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

    # --- outbound email ---
    # Which connector send_email hands the message to (app/core/email.py). Both are
    # always compiled in; this picks one. "smtp" is the original path and stays the
    # default, so an environment that says nothing keeps behaving exactly as before.
    EMAIL_PROVIDER: Literal["smtp", "lettermint"] = "smtp"

    # --- outbound email: SMTP connector ---
    # Any relay works unchanged (Gmail SMTP, AWS SES, Mailgun, Postmark, ...) - just
    # set these in .env. Left unset (the default, e.g. local dev/tests), send_email
    # logs the message instead of sending it, so registering an account never
    # requires real SMTP credentials to work end-to-end.
    SMTP_HOST: str | None = None
    SMTP_PORT: int = 587
    SMTP_USERNAME: str | None = None
    SMTP_PASSWORD: str | None = None
    SMTP_USE_TLS: bool = True
    # Also the sender identity of the Lettermint connector unless LETTERMINT_FROM_*
    # overrides it - see there for when the two have to differ.
    SMTP_FROM_EMAIL: str = "no-reply@poulse.com"
    SMTP_FROM_NAME: str = "Peerkola"

    # --- outbound email: Lettermint connector ---
    # https://lettermint.co - an EU-hosted transactional provider, driven through its
    # official SDK (`lettermint` on PyPI). Reached only when EMAIL_PROVIDER is
    # "lettermint"; every value below is ignored otherwise.
    #
    # The token is a *sending* token (the SDK's default auth scheme), not a team API
    # token. It is required whenever the connector is selected - see
    # `require_token_for_lettermint` below for why a missing one is a startup
    # failure rather than a quiet fall back to logging.
    # `validate_default` so the check below still runs when the env omits the
    # variable entirely, which is exactly the case it exists to catch.
    LETTERMINT_API_TOKEN: str | None = Field(default=None, validate_default=True)

    @field_validator("LETTERMINT_API_TOKEN")
    @classmethod
    def require_token_for_lettermint(
        cls, v: str | None, info: dict[str, Any]
    ) -> str | None:
        """A selected email connector must actually be able to send.

        Deliberately a startup failure rather than a fall back to the SMTP path's
        log-the-message behaviour. That fallback is safe precisely because *not*
        configuring SMTP is the default state, so it can only fire where nobody has
        asked for real mail. Naming a connector is the opposite: an explicit act
        whose only purpose is delivery. Left to degrade quietly it would print
        verification codes into a production log stream - the one thing
        app/core/logger.py says never to log - and the failure would be invisible
        until a user reported that no code ever arrived. A crash on deploy is the
        loud version of the same information.

        A field validator rather than a model one, for the same reason: pydantic
        puts the *validated input* in the error it raises, and for a model
        validator that input is the whole settings dict - so the crash meant to
        protect a secret would print SECRET_KEY into the log on its way out. Here
        the offending value is this field's own, which is by definition unset.
        EMAIL_PROVIDER is declared above so `info.data` already holds it; pydantic
        fills it in field-definition order.
        """
        if info.data.get("EMAIL_PROVIDER") == "lettermint" and not v:
            raise ValueError(
                'EMAIL_PROVIDER is "lettermint" but LETTERMINT_API_TOKEN is not set'
            )
        return v

    # Unset uses the SDK's own default (https://api.lettermint.co/v1). Here only so a
    # staging or mock endpoint can be pointed at without a code change, mirroring
    # MOLLIE_API_BASE_URL.
    LETTERMINT_API_BASE_URL: str | None = None
    LETTERMINT_TIMEOUT_SECONDS: float = 30.0
    # Lettermint "route" - which configured sending route the message goes out on.
    # Unset lets the account's default route decide, which is what a single-route
    # account wants; set it once transactional and marketing mail are separated.
    LETTERMINT_ROUTE: str | None = None
    # Sender identity for this connector, falling back to the SMTP_FROM_* values so
    # there is nothing extra to set in the normal case. They are separate settings
    # because a from-address is provider-scoped: Lettermint only accepts a domain
    # verified inside *its* account, so on the day that domain differs from whatever
    # the SMTP relay was allowed to send as, one shared setting could not express
    # both.
    LETTERMINT_FROM_EMAIL: str | None = None
    LETTERMINT_FROM_NAME: str | None = None

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
    #
    # The names are not ours to choose. AWS_ENDPOINT_URL, S3_BUCKET_NAME,
    # AWS_DEFAULT_REGION, AWS_ACCESS_KEY_ID and AWS_SECRET_ACCESS_KEY are exactly
    # what Railway's bucket auto-connect injects into a service, and what every
    # other S3 client (boto3, the aws CLI, rclone) already reads from the
    # environment - so a Railway Bucket attaches with no variable mapping at all,
    # and a script run inside the container inherits working credentials for free.
    # They used to be STORAGE_*, which meant every one of them had to be wired up
    # by hand in the Railway dashboard. Don't rename them back for tidiness.
    #
    # The remaining four have no S3 convention to follow (they describe how *this*
    # backend addresses the bucket, not how it authenticates), so they carry the
    # S3_ prefix to sit beside the ones that do.
    AWS_ENDPOINT_URL: str = "http://minio:9000"
    # The endpoint *clients* connect to, when it differs from the one the backend
    # uses. It usually does in local dev: the backend reaches MinIO as `minio:9000`
    # on the compose network, while a phone or browser has to reach it as
    # `localhost:9000` (or `10.0.2.2:9000` from the Android emulator). This is not
    # cosmetic - the host is a *signed* header, so a URL signed against one name and
    # rewritten to another is rejected. Unset means "same as AWS_ENDPOINT_URL",
    # which is the correct setting on Railway.
    TEST_S3_PUBLIC_ENDPOINT_URL: str | None = None
    S3_PUBLIC_ENDPOINT_URL: str | None = None

    @field_validator("S3_PUBLIC_ENDPOINT_URL", mode="before")
    @classmethod
    def build_test_s3_public_endpoint(
        cls, v: str | None, info: dict[str, Any]
    ) -> str | None:
        """Under pytest the "client" fetching a presigned URL is the test process,
        which runs *inside* the backend container - so it reaches MinIO as
        `minio:9000`, not as the `localhost:9000` a phone or browser would use.
        Without this swap every media assertion would fail on a connection error
        that has nothing to do with the code under test.
        """
        if "pytest" in sys.modules:
            return info.data.get("TEST_S3_PUBLIC_ENDPOINT_URL") or v
        return v

    # Declared before S3_BUCKET_NAME so the validator below can see it: pydantic
    # fills `info.data` in field-definition order.
    TEST_S3_BUCKET_NAME: str | None = None
    S3_BUCKET_NAME: str = "peerkola-media"

    @field_validator("S3_BUCKET_NAME", mode="before")
    @classmethod
    def build_test_s3_bucket_name(cls, v: str | None, info: dict[str, Any]) -> str:
        """Swaps in TEST_S3_BUCKET_NAME while pytest is running, mirroring what
        DATABASE_URL/REDIS_URL already do - so a test run can never write objects
        into (or delete objects out of) the bucket the dev app is using.
        """
        if "pytest" in sys.modules:
            test_bucket = info.data.get("TEST_S3_BUCKET_NAME")
            if not test_bucket:
                raise ValueError(
                    "pytest detected, but TEST_S3_BUCKET_NAME is not set in environment"
                )
            return str(test_bucket)
        return v

    AWS_DEFAULT_REGION: str = "us-east-1"  # Railway/Tigris wants "auto"
    AWS_ACCESS_KEY_ID: str = ""
    AWS_SECRET_ACCESS_KEY: str = ""
    # "path" -> http://host/<bucket>/<key>, "virtual" -> https://<bucket>.host/<key>.
    # MinIO on localhost can only do path-style (there is no wildcard DNS for
    # `<bucket>.localhost`); Railway/Tigris serves virtual-hosted URLs.
    S3_ADDRESSING_STYLE: str = "virtual"
    # Local dev and CI only: create the bucket on boot (and before a migration that
    # needs it) if it is missing. On Railway the platform provisions the bucket and
    # the credentials are scoped to it, so this stays off there.
    S3_AUTO_CREATE_BUCKET: bool = False

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
    # ffmpeg/ffprobe are the heaviest thing this process runs (hundreds of MB of
    # RSS per transcode), and a verified user may upload several clips per post
    # several times a minute - so their concurrency is capped process-wide with a
    # semaphore (app/core/media_validation.py) and each transcode is pinned to a
    # few threads rather than every core. Uploads past the cap wait their turn
    # rather than failing; the transcode timeout does not start until they run.
    MEDIA_MAX_CONCURRENT_FFMPEG: int = 2
    MEDIA_TRANSCODE_THREADS: int = 2

    # Sanity cap on total blocks per post (text + media combined, see PostBlock) -
    # guards against a pathological submission (thousands of tiny blocks), not a
    # real authoring limit.
    POST_BLOCKS_MAX_COUNT: int = 40
    # Upper bound on the text of one block. A sanity cap like the one above, not an
    # authoring limit - a post is meant to be read on a phone.
    POST_BLOCK_TEXT_MAX_LENGTH: int = 10_000

    # --- profile fields ---
    # Bounds on free text a user writes about themselves. `username` is serialized
    # into every recipient's feed for every non-anonymous post, so an unbounded
    # one is a bandwidth problem for everyone else; derived usernames are shorter
    # still (app/core/username.py: MAX_LENGTH). The minimum matches the app's
    # username fields; the character rule is app/core/username_policy.py.
    # Changing either bound does not revisit existing names - alembic 0007 pinned
    # its own copy when it rewrote them.
    USERNAME_MIN_LENGTH: int = 3
    USERNAME_MAX_LENGTH: int = 30
    BIO_MAX_LENGTH: int = 500

    # --- list routes ---
    # The most rows any list route returns per page, whatever `limit` asks for.
    # Clamped rather than refused, so a client asking for more simply pages. The
    # self-serve lists (own posts, own reviews) presign every attachment of every
    # row they return, which is what makes an unbounded page worth stopping.
    LIST_MAX_PAGE_SIZE: int = 100

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

    @field_validator("SECRET_KEY")
    @classmethod
    def require_real_secret_key(cls, v: str) -> str:
        """Refuse to boot on a key that signs nothing worth trusting.

        Every JWT is HS256 over this value, so whoever knows it mints a superuser
        token. `env-template` ships it as `CHANGE_ME` so a copy of the template
        is not a working secret, and this is what turns forgetting to change it
        into a crash on deploy instead of an open door that looks exactly like a
        healthy service.

        A *field* validator, like `require_token_for_lettermint` and for the same
        reason - a model validator's error would quote the whole settings dict,
        this one quotes only the offending key, which by construction is one
        nobody should be using. The error text names no value even so.
        """
        if len(v) < 32 or v.strip().lower() in _PLACEHOLDER_SECRETS:
            raise ValueError(
                "SECRET_KEY must be at least 32 characters and not a placeholder "
                "(generate one with `openssl rand -hex 32`)"
            )
        return v


#: Values a SECRET_KEY is refused for outright, whatever their length.
_PLACEHOLDER_SECRETS = frozenset(
    {"change_me", "changeme", "change-me", "secret", "secret_key", "secretkey"}
)


settings = Settings()
