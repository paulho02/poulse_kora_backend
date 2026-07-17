from datetime import date as date_cls
from datetime import datetime, timedelta, timezone

from fastapi import APIRouter
from sqlalchemy import Date, cast, func, select

from app.core.config import settings
from app.core.relay_rules import (
    compute_avg_hops,
    compute_badges,
    compute_trust_score,
    is_review_gate_unlocked,
)
from app.deps.db import CurrentAsyncSession
from app.deps.users import CurrentUser
from app.models.post import Post
from app.models.post_review import PostReview
from app.schemas.stats import UserStatsRead, WeeklyActivityBucket

router = APIRouter(prefix="/stats")


@router.get("/me", response_model=UserStatsRead)
async def get_my_stats(
    session: CurrentAsyncSession,
    user: CurrentUser,
):
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

    trust_score = compute_trust_score(user)

    return UserStatsRead(
        reviewed_count=user.reviewed_count,
        forwarded_count=user.forwarded_count,
        dropped_count=user.dropped_count,
        created_post_count=created_post_count or 0,
        trust_score=trust_score,
        avg_hops=compute_avg_hops(user),
        weekly_activity=weekly_activity,
        badges=compute_badges(user, trust_score),
        review_gate=settings.RELAY_REVIEW_GATE,
        unlocked=is_review_gate_unlocked(user),
    )
