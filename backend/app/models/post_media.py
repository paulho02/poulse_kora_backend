from datetime import datetime
from typing import TYPE_CHECKING

from sqlalchemy.orm import Mapped, mapped_column, relationship
from sqlalchemy.sql.functions import func
from sqlalchemy.sql.schema import ForeignKey, Index
from sqlalchemy.sql.sqltypes import DateTime, String

from app.core.config import settings
from app.db import Base

if TYPE_CHECKING:
    from app.models.post import Post


class PostMedia(Base):
    """One attached image or video's bytes. Where it sits in the post's display
    order is *not* stored here - see `PostBlock`, which references this row's `id`
    from whichever position in the post it belongs at.

    Bytes are stored in-DB (`data`), the same stopgap as `User.profile_picture` -
    see the POST_MEDIA_* settings in app/core/config.py for why, and the size/type
    caps enforced on upload. `content_type` and `duration_seconds` are never taken
    from the client as-is: app/core/media_validation.py re-derives content_type from
    what Pillow/ffprobe actually parsed the bytes as, and measures duration_seconds
    itself via ffprobe - both close the "don't trust a client-supplied header" gap.
    """

    __tablename__ = "post_media"
    __table_args__ = (Index("ix_post_media_post_id", "post_id"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    post_id: Mapped[int] = mapped_column(ForeignKey("posts.id"))

    media_type: Mapped[str] = mapped_column(String(10))  # "image" | "video"
    content_type: Mapped[str]
    data: Mapped[bytes]
    size_bytes: Mapped[int]
    # Video only. ffprobe-measured (see media_validation.py), not client-reported.
    duration_seconds: Mapped[float | None]

    created: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )

    post: Mapped["Post"] = relationship(back_populates="media")

    @property
    def url(self) -> str:
        """URL to fetch this item's bytes (`GET /posts/{post_id}/media/{id}`) - what
        actually gets exposed on PostMediaRead, never the bytes themselves. See
        User.profile_picture_url for why (a feed lists many posts; embedding bytes
        would repeat every image/video on every response).
        """
        return f"{settings.API_PATH}/posts/{self.post_id}/media/{self.id}"
