from datetime import datetime
from typing import TYPE_CHECKING

from fastapi_users_db_sqlalchemy import SQLAlchemyBaseUserTableUUID
from sqlalchemy import DateTime
from sqlalchemy.orm import Mapped, mapped_column, relationship
from sqlalchemy.sql.functions import func

from app.db import Base

if TYPE_CHECKING:
    from app.models.channel_subscription import ChannelSubscription  # noqa: F401
    from app.models.item import Item  # noqa: F401
    from app.models.post import Post  # noqa: F401
    from app.models.post_review import PostReview  # noqa: F401


class User(SQLAlchemyBaseUserTableUUID, Base):
    __tablename__ = "users"

    created: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    updated: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )

    username: Mapped[str | None] = mapped_column(unique=True)
    bio: Mapped[str | None]
    dark_mode: Mapped[bool] = mapped_column(default=False, server_default="false")

    # Denormalized counters, updated transactionally alongside `PostReview` inserts
    # (see app/core/relay_rules.py) so the review-gate check is an O(1) attribute read.
    reviewed_count: Mapped[int] = mapped_column(default=0, server_default="0")
    forwarded_count: Mapped[int] = mapped_column(default=0, server_default="0")
    dropped_count: Mapped[int] = mapped_column(default=0, server_default="0")

    items: Mapped["Item"] = relationship(back_populates="user", cascade="all, delete")
    channel_subscriptions: Mapped[list["ChannelSubscription"]] = relationship(
        back_populates="user", cascade="all, delete"
    )
    posts: Mapped[list["Post"]] = relationship(
        back_populates="author", cascade="all, delete"
    )
    post_reviews: Mapped[list["PostReview"]] = relationship(
        back_populates="user", cascade="all, delete"
    )

    def __repr__(self):
        return f"User(id={self.id!r}, name={self.email!r})"
