from datetime import datetime
from typing import TYPE_CHECKING
from uuid import UUID

from fastapi_users_db_sqlalchemy import GUID
from sqlalchemy.orm import Mapped, mapped_column, relationship
from sqlalchemy.sql.functions import func
from sqlalchemy.sql.schema import ForeignKey
from sqlalchemy.sql.sqltypes import DateTime

from app.db import Base

if TYPE_CHECKING:
    from app.models.channel import Channel
    from app.models.post_review import PostReview
    from app.models.user import User


class Post(Base):
    __tablename__ = "posts"

    id: Mapped[int] = mapped_column(primary_key=True)
    channel_id: Mapped[int] = mapped_column(ForeignKey("channels.id"))
    author_id: Mapped[UUID] = mapped_column(GUID, ForeignKey("users.id"))

    text: Mapped[str]
    has_image: Mapped[bool] = mapped_column(default=False, server_default="false")
    is_anonymous: Mapped[bool] = mapped_column(default=False, server_default="false")

    forwarded_count: Mapped[int] = mapped_column(default=0, server_default="0")
    dropped_count: Mapped[int] = mapped_column(default=0, server_default="0")

    created: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    updated: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )

    channel: Mapped["Channel"] = relationship(back_populates="posts")
    author: Mapped["User"] = relationship(back_populates="posts")
    reviews: Mapped[list["PostReview"]] = relationship(
        back_populates="post", cascade="all, delete"
    )
