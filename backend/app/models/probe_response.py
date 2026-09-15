from datetime import datetime
from typing import TYPE_CHECKING
from uuid import UUID

from fastapi_users_db_sqlalchemy import GUID
from sqlalchemy.orm import Mapped, mapped_column, relationship
from sqlalchemy.sql.functions import func
from sqlalchemy.sql.schema import ForeignKey, Index, UniqueConstraint
from sqlalchemy.sql.sqltypes import Boolean, DateTime, String

from app.db import Base

if TYPE_CHECKING:
    from app.models.post import Post
    from app.models.user import User


class ProbeResponse(Base):
    """How one reader answered one test post - the ground truth Reviewer Trust reads.

    Deliberately *not* a `PostReview` row with a flag on it. A probe is a measurement,
    and a measurement that enters the data it measures stops being one: a `PostReview`
    would feed the review count, the pace signal and the forward-rate skew that
    `app/core/trust.py` computes from real reviewing, so a reader's probes would be
    partly scoring themselves. Keeping them in their own table is what lets
    `reviewed_count == forwarded_count + dropped_count == COUNT(post_reviews)` stay true
    everywhere it is true today, and what keeps probes out of `GET /posts/reviewed`.

    `correct` is stored rather than derived from the two kind columns, because the
    variant catalogue is copy and copy gets edited: if a variant's `expected_kind` were
    ever corrected, deriving would silently rewrite the history of everyone who answered
    the old wording. The two kinds are kept alongside it for exactly that forensic
    reason - they say what was asked and what was done, at the time.

    The `(user_id, created)` index is what makes the score cheap: the window aggregate
    in `trust_service` is a single indexed range scan per reader, which is half of why
    this needs no background job.
    """

    __tablename__ = "probe_responses"
    __table_args__ = (
        UniqueConstraint("user_id", "post_id", name="uq_probe_response_user_post"),
        Index("ix_probe_responses_user_created", "user_id", "created"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[UUID] = mapped_column(GUID, ForeignKey("users.id"))
    post_id: Mapped[int] = mapped_column(ForeignKey("posts.id"))

    # Which entry of app/core/probe_templates.py this was. Kept so the drop rate of an
    # individual variant is answerable: a variant everyone fails is a badly worded
    # variant, not a deployment full of inattentive readers.
    variant_code: Mapped[str] = mapped_column(String(64))

    expected_kind: Mapped[str] = mapped_column(String(10))  # "forward" | "drop"
    given_kind: Mapped[str] = mapped_column(String(10))
    correct: Mapped[bool] = mapped_column(Boolean)

    created: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )

    user: Mapped["User"] = relationship(back_populates="probe_responses")
    post: Mapped["Post"] = relationship()
