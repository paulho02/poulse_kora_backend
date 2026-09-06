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
    # How the post itself has fared, counting this review. Returned *only* here,
    # never on PostRead: revealed after the verdict so it cannot influence it.
    # `post_reviewed_count` is everyone who forwarded or dropped it, i.e. the
    # denominator - "3 of 12" says far more than a bare 3, and both are already
    # on the row, so neither costs a query.
    post_forwarded_count: int
    post_reviewed_count: int


class ReviewedPostRead(BaseModel):
    """A post paired with the viewer's own review of it, for the reviewed-history list."""

    post: PostRead
    kind: ReviewKind
    reviewed_at: datetime
