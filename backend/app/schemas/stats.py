from datetime import date

from pydantic import BaseModel


class WeeklyActivityBucket(BaseModel):
    date: date
    count: int


class BadgeRead(BaseModel):
    code: str
    label: str
    earned: bool


class UserStatsRead(BaseModel):
    reviewed_count: int
    forwarded_count: int
    dropped_count: int
    created_post_count: int
    trust_score: int
    avg_hops: float
    weekly_activity: list[WeeklyActivityBucket]
    badges: list[BadgeRead]
    review_gate: int
    unlocked: bool


class ForwardingDistributionBucket(BaseModel):
    # Bucket label for a number of forwards, e.g. "0", "1", ... "5+".
    label: str
    post_count: int


class GlobalStatsRead(BaseModel):
    total_posts: int
    forwarding_distribution: list[ForwardingDistributionBucket]
