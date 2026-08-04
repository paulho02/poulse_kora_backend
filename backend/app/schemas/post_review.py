from datetime import datetime
from typing import Literal

from pydantic import BaseModel

from app.schemas.post import PostRead

ReviewKind = Literal["forward", "drop"]


class PostReviewCreate(BaseModel):
    kind: ReviewKind


class PostReviewResult(BaseModel):
    post_id: int
    kind: ReviewKind
    reviewed_count: int
    review_gate: int
    unlocked: bool
    # Spendable token balance after earning one token for this review.
    token_balance: int


class ReviewedPostRead(BaseModel):
    """A post paired with the viewer's own review of it, for the reviewed-history list."""

    post: PostRead
    kind: ReviewKind
    reviewed_at: datetime
