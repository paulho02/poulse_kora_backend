from datetime import date as date_cls
from datetime import datetime, timedelta, timezone

from fastapi import APIRouter
from sqlalchemy import Date, cast, func, select

from app.core import trust_service
from app.core.config import settings
from app.core.relay_rules import (
    compute_avg_hops,
    compute_badges,
    is_review_gate_unlocked,
)
from app.deps.db import CurrentAsyncSession
from app.deps.redis import CurrentRedis
from app.deps.users import CurrentVerifiedUser
from app.models.post import Post
from app.models.post_review import PostReview
from app.schemas.stats import (
    ForwardingDistributionBucket,
    GlobalStatsRead,
    UserStatsRead,
    WeeklyActivityBucket,
)

router = APIRouter(prefix="/stats")

# Forwards are bucketed 0,1,2,3,4 and then everything >= this cap into "5+".
_FORWARD_BUCKET_CAP = 5


@router.get("/me", response_model=UserStatsRead)
async def get_my_stats(
    session: CurrentAsyncSession,
    user: CurrentVerifiedUser,
    redis: CurrentRedis,
):
    """This reader's own numbers, including their Reviewer Trust.

    The trust figure is read through `trust_service`, so it comes off the cache in the
    ordinary case and is recomputed at most once per `TRUST_CACHE_TTL_SECONDS`. Worth
    knowing: this is the same value the forward path reads, not a display-only
    recalculation, so what the profile shows is exactly what a forward will be worth.
    """
    today = datetime.now(timezone.utc).date()
    week_ago = datetime.now(timezone.utc) - timedelta(days=7)

    rows = (
        await session.execute(
            select(
                cast(PostReview.created, Date).label("day"),
                func.count().label("cnt"),
            )
            .filter(PostReview.user_id == user.id, PostReview.created >= week_ago)
            .group_by("day")
        )
    ).all()
    counts_by_day: dict[date_cls, int] = {row.day: row.cnt for row in rows}

    weekly_activity = [
        WeeklyActivityBucket(
            date=today - timedelta(days=offset),
            count=counts_by_day.get(today - timedelta(days=offset), 0),
        )
        for offset in range(6, -1, -1)
    ]

    created_post_count = await session.scalar(
        select(func.count(Post.id)).filter(Post.author_id == user.id)
    )

    trust = await trust_service.reviewer_trust(session, redis, str(user.id))

    return UserStatsRead(
        reviewed_count=user.reviewed_count,
        forwarded_count=user.forwarded_count,
        dropped_count=user.dropped_count,
        created_post_count=created_post_count or 0,
        trust_score=trust.score,
        trust_band=trust.band,
        trust_fanout=trust.fanout,
        trust_reach_multiplier=trust.reach_multiplier,
        trust_window_days=trust.window_days,
        avg_hops=compute_avg_hops(user),
        weekly_activity=weekly_activity,
        badges=compute_badges(user, trust.score),
        review_gate=settings.RELAY_REVIEW_GATE,
        unlocked=is_review_gate_unlocked(user),
    )


@router.get("/global", response_model=GlobalStatsRead)
async def get_global_stats(
    session: CurrentAsyncSession,
    user: CurrentVerifiedUser,
):
    """App-wide stats shown to every user.

    For now: the distribution of how many times posts have been forwarded,
    bucketed 0..4 with a final "5+" bucket.
    """
    rows = (
        await session.execute(
            select(
                Post.forwarded_count.label("forwards"),
                func.count(Post.id).label("cnt"),
            )
            # Test posts are never forwarded onward and are minted one per reader, so
            # counting them would pile an ever-growing mound into the "0 forwards"
            # bucket and make the deployment look worse at distributing content the
            # more carefully it measures its readers.
            .filter(Post.is_probe.is_(False))
            .group_by(Post.forwarded_count)
        )
    ).all()

    # Seed every bucket so gaps render as zero-height bars, not missing ones.
    counts = [0] * (_FORWARD_BUCKET_CAP + 1)
    for row in rows:
        counts[min(row.forwards, _FORWARD_BUCKET_CAP)] += row.cnt

    distribution = [
        ForwardingDistributionBucket(
            label=f"{i}+" if i == _FORWARD_BUCKET_CAP else str(i),
            post_count=count,
        )
        for i, count in enumerate(counts)
    ]

    return GlobalStatsRead(
        total_posts=sum(counts),
        forwarding_distribution=distribution,
    )
