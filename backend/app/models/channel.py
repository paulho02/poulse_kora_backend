from datetime import datetime
from typing import TYPE_CHECKING

from sqlalchemy.orm import Mapped, mapped_column, relationship
from sqlalchemy.sql.functions import func
from sqlalchemy.sql.sqltypes import DateTime

from app.db import Base

if TYPE_CHECKING:
    from app.models.channel_subscription import ChannelSubscription
    from app.models.post import Post


class Channel(Base):
    __tablename__ = "channels"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(unique=True)
    color: Mapped[str] = mapped_column(default="#6B7280")
    description: Mapped[str] = mapped_column(default="")

    created: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    updated: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )

    subscriptions: Mapped[list["ChannelSubscription"]] = relationship(
        back_populates="channel", cascade="all, delete"
    )
    posts: Mapped[list["Post"]] = relationship(
        back_populates="channel", cascade="all, delete"
    )
