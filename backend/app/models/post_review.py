from datetime import datetime
from typing import TYPE_CHECKING
from uuid import UUID

from fastapi_users_db_sqlalchemy import GUID
from sqlalchemy.orm import Mapped, mapped_column, relationship
from sqlalchemy.sql.functions import func
from sqlalchemy.sql.schema import ForeignKey, Index, UniqueConstraint
from sqlalchemy.sql.sqltypes import DateTime, String

from app.db import Base

if TYPE_CHECKING:
    from app.models.post import Post
    from app.models.user import User


class PostReview(Base):
    """One row per (user, post) review action (`forward` or `drop`).

    `created` doubles as `reviewed_at` for the weekly-activity stat.
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

    created: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )

    user: Mapped["User"] = relationship(back_populates="post_reviews")
    post: Mapped["Post"] = relationship(back_populates="reviews")
