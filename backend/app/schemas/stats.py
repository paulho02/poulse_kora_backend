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
    # Reviewer Trust, 0-100 (see app/core/trust.py). Measured over the trailing
    # `trust_window_days` only, so it is a statement about recent reading rather than a
    # lifetime record - which is also why a returning account finds it back at neutral.
    trust_score: int
    # Which reach band the score falls in: "low" | "normal" | "high". A stable code
    # rather than a label, so the client's .arb supplies the words (the same contract
    # `api_error` uses) and the bands can be renamed without a backend release.
    trust_band: str
    # Recipients one of this reader's forwards reaches, and that as a multiple of the
    # standard fan-out. Sent as numbers because the explainer slide says what the score
    # *does*, and "your forwards reach 4 people instead of 3" is the only form of that
    # sentence a reader can act on.
    trust_fanout: int
    trust_reach_multiplier: float
    trust_window_days: int
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
