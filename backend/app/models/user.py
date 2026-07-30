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

    # Flipped once, after the mobile app's one-time onboarding flow (intro slides,
    # mandatory channel picks, disclaimer) is confirmed. A plain one-way flag: unlike
    # `dark_mode` it has no offline-conflict scenario, so it isn't part of
    # SETTINGS_FIELDS/settings_revision.
    onboarding_completed: Mapped[bool] = mapped_column(
        default=False, server_default="false"
    )

    # Bumped by UserManager._update whenever a *settings* field (see SETTINGS_FIELDS
    # in app/deps/users.py) actually changes value. The mobile app keeps the revision
    # it last reconciled with, so it can tell "nobody else touched this, safe to push
    # my offline change" from "another device changed it too".
    #
    # This exists instead of reusing `updated` because `updated` is a whole-row
    # onupdate: reviewing a post bumps reviewed_count and would therefore look like a
    # settings conflict on every single forward/drop. It also avoids comparing a
    # server clock against a device clock.
    settings_revision: Mapped[int] = mapped_column(default=0, server_default="0")

    # Denormalized counters, updated transactionally alongside `PostReview` inserts
    # (see app/core/relay_rules.py) so the review-gate check is an O(1) attribute read.
    reviewed_count: Mapped[int] = mapped_column(default=0, server_default="0")
    forwarded_count: Mapped[int] = mapped_column(default=0, server_default="0")
    dropped_count: Mapped[int] = mapped_column(default=0, server_default="0")

    items: Mapped[list["Item"]] = relationship(
        back_populates="user", cascade="all, delete"
    )
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
