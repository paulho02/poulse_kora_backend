from datetime import datetime
from typing import TYPE_CHECKING

from sqlalchemy.orm import Mapped, mapped_column, relationship
from sqlalchemy.sql.functions import func
from sqlalchemy.sql.schema import ForeignKey, Index, UniqueConstraint
from sqlalchemy.sql.sqltypes import DateTime, String

from app.db import Base

if TYPE_CHECKING:
    from app.models.post import Post
    from app.models.post_media import PostMedia


class PostBlock(Base):
    """One paragraph of text or one attached image/video, in `Post.blocks`'s display
    order - together these are the post's content, article-style: text and media
    interleaved in whatever order the author arranged them in, rather than a text
    blob with a media strip bolted on the end.

    Exactly one of `text`/`media_id` is set, matching `block_type`. A media block
    only ever *references* a `PostMedia` row (created alongside it in the same
    request, see api/posts.py:create_post) - the bytes/content_type/etc. still live
    entirely on `PostMedia`, this just says *where in the post* that item sits.
    """

    __tablename__ = "post_blocks"
    __table_args__ = (
        Index("ix_post_blocks_post_id", "post_id"),
        UniqueConstraint("post_id", "position", name="uq_post_block_post_position"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    post_id: Mapped[int] = mapped_column(ForeignKey("posts.id"))

    position: Mapped[int]
    block_type: Mapped[str] = mapped_column(String(10))  # "text" | "media"
    text: Mapped[str | None]
    media_id: Mapped[int | None] = mapped_column(ForeignKey("post_media.id"))

    created: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )

    post: Mapped["Post"] = relationship(back_populates="blocks")
    media: Mapped["PostMedia | None"] = relationship()
