from typing import Literal

from pydantic import BaseModel

ReviewKind = Literal["forward", "drop"]


class PostReviewCreate(BaseModel):
    kind: ReviewKind


class PostReviewResult(BaseModel):
    post_id: int
    kind: ReviewKind
    reviewed_count: int
    review_gate: int
    unlocked: bool
