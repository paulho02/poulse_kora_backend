from datetime import datetime
from typing import Literal

from pydantic import BaseModel, model_validator

from app.schemas.post import PostRead

ReviewKind = Literal["forward", "drop"]


class PostReviewCreate(BaseModel):
    kind: ReviewKind
    # Forward, and hand the token this review earns to the post's author instead of
    # keeping it. Only a forward can carry one: a gift is praise, and praise for a post
    # you are burying is a contradiction the economy has no use for.
    gift_token: bool = False

    @model_validator(mode="after")
    def _gift_only_with_forward(self) -> "PostReviewCreate":
        if self.gift_token and self.kind != "forward":
            raise ValueError("gift_token requires kind 'forward'")
        return self


class PostReviewResult(BaseModel):
    post_id: int
    kind: ReviewKind
    reviewed_count: int
    review_gate: int
    unlocked: bool
    # Spendable token balance after earning one token for this review - or, when the
    # review gifted it, unchanged, because that token went to the author.
    token_balance: int
    # Whether this review's token went to the author (see PostReviewCreate.gift_token).
    gifted: bool = False
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
    gifted: bool = False
    reviewed_at: datetime
