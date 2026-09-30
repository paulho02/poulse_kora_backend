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


class PostReview(Base):
    """One row per (user, post) review action (`forward` or `drop`).

    `created` doubles as `reviewed_at` for the weekly-activity stat.

    `gifted` marks a forward whose earned token went to the post's author instead of
    the reviewer. Kept here rather than only in Redis because `rebuild_redis` re-derives
    every balance from Postgres, and a gift moves a token between two of them.
    """

    __tablename__ = "post_reviews"
    __table_args__ = (
        UniqueConstraint("user_id", "post_id", name="uq_post_review_user_post"),
        Index("ix_post_reviews_user_created", "user_id", "created"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[UUID] = mapped_column(GUID, ForeignKey("users.id"))
    post_id: Mapped[int] = mapped_column(ForeignKey("posts.id"))
    kind: Mapped[str] = mapped_column(String(10))
    gifted: Mapped[bool] = mapped_column(
        Boolean, default=False, server_default="false"
    )

    created: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )

    user: Mapped["User"] = relationship(back_populates="post_reviews")
    post: Mapped["Post"] = relationship(back_populates="reviews")
