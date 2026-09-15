"""Gathering the inputs `app/core/trust.py` scores, and caching the result.

The split is the same one `app/feed/pricing.py` and `app/feed/service.py` use: the
formula is pure and testable over a table of numbers, and everything that has to touch
Postgres or Redis lives here.

**Why this is lazy and cached rather than a background job.** A periodic recompute costs
work proportional to the number of *accounts*, whether or not any of them are reading;
this costs work proportional to *activity*, and a score is only ever needed for a reader
who is actually forwarding something or looking at their profile. The computation itself
is two indexed aggregates and one deployment-wide number that is shared by everybody, so
there is nothing heavy for a job to amortise - it would buy staleness without buying
speed. The cache is what keeps a burst of forwards from repeating the aggregates, and
answering a forward is one Redis GET in the ordinary case.

Three details are load-bearing:

- **Every aggregate is computed in SQL, not in Python.** The pace signal needs the gap
  between consecutive reviews, which is a `lag()` window function over the existing
  `ix_post_reviews_user_created` index - so a reader with four thousand reviews in the
  window still returns exactly one row. Fetching the timestamps to diff them here would
  make the cost of scoring someone proportional to how much they read, which is the
  wrong way round for a feature meant to reward reading.

- **The population forward rate is cached separately and far longer.** It is one number
  shared by every reader, it barely moves, and it is the only part of the computation
  that looks at more than one account's rows. Below `TRUST_FORWARD_RATE_MIN_SAMPLE` it
  is reported as None rather than as a number, which switches the skew signal off
  entirely - a deployment too young to have a consensus has nothing to be anomalous
  against, and inventing one would manufacture the very signal this is supposed to
  observe.

- **Answering a probe invalidates the cache immediately.** Probes are the strongest and
  rarest input, so a reader who has just passed or failed one should see the effect
  rather than wait out a ten-minute TTL. Nothing else invalidates: an ordinary review
  moves the score by a fraction of a point.
"""

import json
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from redis.asyncio import Redis
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core import trust
from app.core.config import settings
from app.core.logger import get_logger
from app.models.post_review import PostReview
from app.models.probe_response import ProbeResponse

log = get_logger(__name__)

# Per-reader cached score: {"score", "band", "computed_at"} JSON.
def cache_key(user_id: str) -> str:
    return f"trust:reviewer:{user_id}"


# The deployment's forward rate for the current window: {"rate", "reviews"} JSON, or a
# stored null rate when the sample is too thin to mean anything. One key for everyone.
FORWARD_RATE_KEY = "trust:forward_rate"


@dataclass(frozen=True)
class TrustResult:
    """A reader's Reviewer Trust, and everything the client and the feed read off it."""

    score: int
    band: str
    fanout: int
    reach_multiplier: float
    window_days: int


def _result(score: int) -> TrustResult:
    band = trust.band_for(score, settings)
    return TrustResult(
        score=score,
        band=band,
        fanout=trust.fanout_for(band, settings),
        reach_multiplier=trust.reach_multiplier(band, settings),
        window_days=settings.TRUST_WINDOW_DAYS,
    )


def window_start(now: datetime | None = None) -> datetime:
    """The beginning of the trailing window every input is measured over."""
    if now is None:
        now = datetime.now(timezone.utc)
    return now - timedelta(days=settings.TRUST_WINDOW_DAYS)


async def _review_aggregates(session: AsyncSession, user_id: str, since: datetime):
    """`(reviews, forwards, paced, hasty)` for one reader over the window, in one query.

    The inner select stamps each review with the seconds since the reader's previous
    one; the outer one counts. `gap` is NULL for the first review in the window (no
    predecessor), and `NULL < x` is NULL in SQL, so that row is excluded from both
    `paced` and `hasty` without a special case - which is why `paced` is its own count
    rather than `reviews - 1`.
    """
    gaps = (
        select(
            PostReview.kind.label("kind"),
            (
                func.extract("epoch", PostReview.created)
                - func.extract(
                    "epoch",
                    func.lag(PostReview.created).over(order_by=PostReview.created),
                )
            ).label("gap"),
        )
        .where(PostReview.user_id == user_id, PostReview.created >= since)
        .subquery()
    )
    row = (
        await session.execute(
            select(
                func.count().label("reviews"),
                func.count().filter(gaps.c.kind == "forward").label("forwards"),
                func.count().filter(gaps.c.gap.is_not(None)).label("paced"),
                func.count()
                .filter(gaps.c.gap < settings.TRUST_MIN_READ_SECONDS)
                .label("hasty"),
            ).select_from(gaps)
        )
    ).one()
    return row.reviews or 0, row.forwards or 0, row.paced or 0, row.hasty or 0


async def _probe_aggregates(session: AsyncSession, user_id: str, since: datetime):
    """`(answered, correct)` for one reader over the window, in one query."""
    row = (
        await session.execute(
            select(
                func.count().label("answered"),
                func.count()
                .filter(ProbeResponse.correct.is_(True))
                .label("correct"),
            ).where(
                ProbeResponse.user_id == user_id, ProbeResponse.created >= since
            )
        )
    ).one()
    return row.answered or 0, row.correct or 0


async def population_forward_rate(
    session: AsyncSession, redis: Redis
) -> float | None:
    """The deployment's forward rate over the window, or None when too thin to use.

    Cached for `TRUST_FORWARD_RATE_TTL_SECONDS` because it is the same number for every
    reader and the only aggregate here that reads other people's rows. A miss recomputes
    it; a Redis failure is not caught, for the same reason nothing else in this path
    catches one - a scoring feature degrading silently into "everyone is neutral" is
    worse than a visible error.

    None below the minimum sample is not a fallback, it is the answer: `trust.skew_-
    penalty` treats it as "no signal" and contributes nothing, so a young deployment
    scores purely on probes and volume until it has a population to compare against.
    """
    raw = await redis.get(FORWARD_RATE_KEY)
    if raw is not None:
        cached = json.loads(raw)
        return cached["rate"]

    since = window_start()
    row = (
        await session.execute(
            select(
                func.count().label("reviews"),
                func.count().filter(PostReview.kind == "forward").label("forwards"),
            ).where(PostReview.created >= since)
        )
    ).one()
    reviews = row.reviews or 0
    rate = (
        (row.forwards or 0) / reviews
        if reviews >= settings.TRUST_FORWARD_RATE_MIN_SAMPLE
        else None
    )
    await redis.set(
        FORWARD_RATE_KEY,
        json.dumps({"rate": rate, "reviews": reviews}),
        ex=settings.TRUST_FORWARD_RATE_TTL_SECONDS,
    )
    # DEBUG, not INFO: it fires once per TTL per process, and it is only ever read
    # while asking why a deployment's scores look the way they do.
    log.debug("trust.forward_rate_refreshed", rate=rate, reviews=reviews)
    return rate


async def compute(session: AsyncSession, redis: Redis, user_id: str) -> TrustResult:
    """Score one reader from scratch, ignoring (and not writing) the cache."""
    since = window_start()
    reviews, forwards, paced, hasty = await _review_aggregates(
        session, user_id, since
    )
    answered, correct = await _probe_aggregates(session, user_id, since)
    inputs = trust.TrustInputs(
        probes_answered=answered,
        probes_correct=correct,
        reviews=reviews,
        forwards=forwards,
        paced_reviews=paced,
        hasty_reviews=hasty,
        population_forward_rate=await population_forward_rate(session, redis),
    )
    return _result(trust.compute_score(inputs, settings))


async def reviewer_trust(
    session: AsyncSession, redis: Redis, user_id: str
) -> TrustResult:
    """One reader's Reviewer Trust, from cache when it is warm.

    The band and the fan-out are recomputed from the cached *score* rather than cached
    alongside it, so changing `TRUST_BAND_*` or `FEED_FANOUT` takes effect on deploy
    instead of trickling in as entries expire.
    """
    key = cache_key(user_id)
    raw = await redis.get(key)
    if raw is not None:
        return _result(int(json.loads(raw)["score"]))

    result = await compute(session, redis, user_id)
    await redis.set(
        key,
        json.dumps({"score": result.score, "computed_at": time.time()}),
        ex=settings.TRUST_CACHE_TTL_SECONDS,
    )
    return result


async def invalidate(redis: Redis, user_id: str) -> None:
    """Drop a reader's cached score, so the next read recomputes it."""
    await redis.delete(cache_key(user_id))


async def forward_fanout(
    session: AsyncSession, redis: Redis, user_id: str
) -> int | None:
    """Recipients this reader's forward should reach, or None to leave it to the worker.

    None when the feature is off, which is what keeps the disabled path *identical* to
    the pre-trust one: no `fanout` field is written on the stream entry, and the worker
    falls back to `settings.FEED_FANOUT` exactly as it does for an operation minted
    before this existed.
    """
    if not settings.TRUST_ENABLED:
        return None
    return (await reviewer_trust(session, redis, user_id)).fanout
