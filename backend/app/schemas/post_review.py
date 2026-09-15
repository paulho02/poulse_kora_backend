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
    # True when the post just reviewed was a test (see app/core/probes.py). The client
    # shows a "that was a check" confirmation in place of the forward-score badge, which
    # would be meaningless here: a probe is minted for one reader and nobody else will
    # ever see it, so its counters can only ever read 1 of 1.
    is_probe: bool = False
    # Whether the test was answered as it asked. None for an ordinary post, and also for
    # a probe whose wording no longer matches any variant in the catalogue - there is no
    # expected verdict left to compare against, so nothing is scored and nothing is
    # claimed. Told to the reader on purpose: getting one wrong should be visible, or
    # the only feedback a careless reader ever gets is reach quietly disappearing.
    probe_correct: bool | None = None


class ReviewedPostRead(BaseModel):
    """A post paired with the viewer's own review of it, for the reviewed-history list."""

    post: PostRead
    kind: ReviewKind
    reviewed_at: datetime
