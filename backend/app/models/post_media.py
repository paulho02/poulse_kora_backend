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
    caps enforced on upload. `content_type`, `duration_seconds` and `width`/`height`
    are never taken from the client as-is: app/core/media_validation.py re-derives
    content_type from what Pillow/ffprobe actually parsed the bytes as, and measures
    the rest itself - all closing the "don't trust a client-supplied header" gap.

    Everything uploaded from now on is one of two fixed aspect ratios
    (POST_MEDIA_LANDSCAPE_RATIO / POST_MEDIA_PORTRAIT_RATIO), but the ratio columns
    are still nullable: rows written before those existed have no measurements and
    are not backfilled, so a client must treat missing `width`/`height` as "unknown
    shape, letterbox it" rather than assuming either ratio.
    """

    __tablename__ = "post_media"
    __table_args__ = (Index("ix_post_media_post_id", "post_id"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    post_id: Mapped[int] = mapped_column(ForeignKey("posts.id"))

    media_type: Mapped[str] = mapped_column(String(10))  # "image" | "video"
    content_type: Mapped[str]
    # `deferred`, unlike every other column here: a feed response serializes many
    # posts and needs only each attachment's *metadata*, while these two hold the
    # whole image/video. Left undeferred, one `GET /posts/feed` dragged every
    # attached clip's bytes (up to POST_VIDEO_MAX_BYTES each) out of Postgres and
    # through the ORM only to throw them away. The two routes that actually serve
    # bytes undefer explicitly (see get_post_media / get_post_media_poster) - a
    # plain attribute access on a deferred column would emit a lazy load, which
    # raises under asyncio rather than silently working.
    data: Mapped[bytes] = mapped_column(deferred=True)
    size_bytes: Mapped[int]
    # Video only. ffprobe-measured (see media_validation.py), not client-reported.
    duration_seconds: Mapped[float | None]
    # Pixel size of `data`. Nullable only for rows predating this column - see the
    # class docstring. Exposed so a client can reserve the right box for a media
    # block *before* the bytes arrive, instead of laying out a fixed-height
    # placeholder and reflowing once it can measure the image itself.
    width: Mapped[int | None]
    height: Mapped[int | None]

    # Video only: a still frame from the clip, at the same aspect ratio, shown in
    # place of a black rectangle until playback starts. Nullable both for rows
    # predating it and because extraction is deliberately non-fatal (a video that
    # transcodes but yields no frame is still a good video).
    poster: Mapped[bytes | None] = mapped_column(deferred=True)
    # Doubles as the "is there a poster?" flag (see `poster_url`) precisely because
    # it is *not* deferred - asking `poster is None` would defeat the deferral.
    poster_content_type: Mapped[str | None]

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

    @property
    def poster_url(self) -> str | None:
        """URL for `poster`'s bytes, or None when there is no poster frame. Served
        by its own route rather than inlined for the same reason as `url`, and it
        matters more here: a feed card shows a video's poster without ever touching
        the clip, so this is the *only* thing a scrolling list fetches for a video.
        """
        if self.poster_content_type is None:
            return None
        return f"{settings.API_PATH}/posts/{self.post_id}/media/{self.id}/poster"
