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
