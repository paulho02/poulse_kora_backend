from datetime import datetime
from typing import TYPE_CHECKING

from sqlalchemy.orm import Mapped, mapped_column, relationship
from sqlalchemy.sql.functions import func
from sqlalchemy.sql.schema import ForeignKey, Index
from sqlalchemy.sql.sqltypes import DateTime, String

from app.core.storage import storage
from app.db import Base

if TYPE_CHECKING:
    from app.models.post import Post


class PostMedia(Base):
    """One attached image or video. Where it sits in the post's display order is
    *not* stored here - see `PostBlock`, which references this row's `id` from
    whichever position in the post it belongs at.

    The bytes live in the media bucket (app/core/storage.py); this row keeps the
    object key plus exactly the metadata a feed needs to lay the block out before
    the bytes arrive. They used to live in Postgres, which is why several things
    that were once load-bearing are gone: `data`/`poster` no longer need to be
    deferred columns (a feed query cannot drag a video through the ORM if the
    video was never in the database), and app/core/http_range.py is gone with them
    - a player's `Range` requests are now answered by the bucket, which actually
    streams, rather than by slicing a fully-loaded in-memory buffer.

    `content_type`, `duration_seconds` and `width`/`height` are still never taken
    from the client as-is: app/core/media_validation.py re-derives content_type
    from what Pillow/ffprobe actually parsed the bytes as, and measures the rest
    itself - closing the "don't trust a client-supplied header" gap.

    Everything uploaded from now on is one of two fixed aspect ratios
    (POST_MEDIA_LANDSCAPE_RATIO / POST_MEDIA_PORTRAIT_RATIO), but the ratio columns
    stay nullable and a client must treat missing `width`/`height` as "unknown shape,
    letterbox it" rather than assuming either ratio. Rows written before those columns
    existed are repairable rather than stuck, though: scripts/safe/backfill_post_media.py re-runs the
    upload pipeline over the stored object to fill in the measurements and the poster.
    """

    __tablename__ = "post_media"
    __table_args__ = (Index("ix_post_media_post_id", "post_id"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    post_id: Mapped[int] = mapped_column(ForeignKey("posts.id"))

    media_type: Mapped[str] = mapped_column(String(10))  # "image" | "video"
    content_type: Mapped[str]
    # Key of the object in the media bucket. Random and flat (see
    # storage.post_media_key), carrying neither the post id nor the author id: an
    # anonymous post's URL is visible to every recipient, so a key derived from the
    # author would deanonymize exactly what `is_anonymous` exists to hide.
    object_key: Mapped[str]
    size_bytes: Mapped[int]
    # Video only. ffprobe-measured (see media_validation.py), not client-reported.
    duration_seconds: Mapped[float | None]
    # Pixel size of the stored object. Nullable only for rows predating this column -
    # see the class docstring. Exposed so a client can reserve the right box for a
    # media block *before* the bytes arrive, instead of laying out a fixed-height
    # placeholder and reflowing once it can measure the image itself.
    width: Mapped[int | None]
    height: Mapped[int | None]

    # Video only: a still frame from the clip, at the same aspect ratio, shown in
    # place of a black rectangle until playback starts. Its own object, so a feed
    # card can fetch the preview without touching the clip. Null both for images and
    # because extraction is deliberately non-fatal (a video that transcodes but
    # yields no frame is still a good video); doubles as the "is there a poster?"
    # flag behind `poster_url`.
    poster_object_key: Mapped[str | None]

    created: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )

    post: Mapped["Post"] = relationship(back_populates="media")

    @property
    def url(self) -> str:
        """A short-lived presigned URL for this item's bytes - what gets exposed on
        PostMediaRead. It points at the bucket, not at this backend, so the bytes
        never pass through this process; the signature is what authorizes the fetch,
        and it is only minted after `_can_view_post` has already said yes (see
        app/api/posts.py). See app/core/storage.py for the tradeoff that involves.
        """
        return storage.presigned_url(self.object_key)

    @property
    def poster_url(self) -> str | None:
        """URL for the poster frame, or None when there is no poster.

        Matters more than `url` on a feed: a card shows a video's poster without
        ever fetching the clip, so for a video this is the only object a scrolling
        list touches.
        """
        if self.poster_object_key is None:
            return None
        return storage.presigned_url(self.poster_object_key)
