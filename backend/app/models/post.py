from datetime import datetime
from typing import TYPE_CHECKING
from uuid import UUID

from fastapi_users_db_sqlalchemy import GUID
from sqlalchemy.orm import Mapped, mapped_column, relationship
from sqlalchemy.sql.functions import func
from sqlalchemy.sql.schema import ForeignKey, Index
from sqlalchemy.sql.sqltypes import DateTime, String

from app.core.config import LANGUAGE_UNSPECIFIED
from app.db import Base

if TYPE_CHECKING:
    from app.models.channel import Channel
    from app.models.post_block import PostBlock
    from app.models.post_media import PostMedia
    from app.models.post_review import PostReview
    from app.models.user import User


class Post(Base):
    __tablename__ = "posts"
    __table_args__ = (
        Index("ix_posts_author_created", "author_id", "created"),
        Index("ix_posts_probe_created", "is_probe", "created"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    channel_id: Mapped[int] = mapped_column(ForeignKey("channels.id"))
    # NULL once the author has deleted their account but chose to leave their
    # posts in circulation (see app/core/account_deletion.py). Genuine erasure
    # rather than a tombstone user row: the account and its username are gone,
    # and the post renders with no author at all - `_serialize_post` in
    # app/api/posts.py answers the same empty `PostAuthor` it already answers
    # for an anonymous post, so nothing reader-side had to learn a third case.
    #
    # `ondelete="SET NULL"` is the backstop, not the mechanism: the deletion
    # path nulls these rows explicitly before it deletes the user, because the
    # *other* branch (erase the posts too) has to run first and neither branch
    # can be expressed as a single DB rule. Same shape as `Feedback.user_id`.
    author_id: Mapped[UUID | None] = mapped_column(
        GUID, ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )

    is_anonymous: Mapped[bool] = mapped_column(default=False, server_default="false")

    # A test post: content that asks, in its own words, to be forwarded or dropped, so
    # that Reviewer Trust has one signal a script cannot fake (see app/core/probes.py).
    #
    # A real Post row rather than a synthetic id or a parallel table, because it then
    # renders, opens, and is reviewed through exactly the paths a real post is - there
    # is no second serializer to keep in step, and nothing about how it arrives can give
    # it away. What differs is entirely downstream of this flag: reviewing one writes a
    # ProbeResponse instead of a PostReview, forwarding one mints no fan-out operation,
    # and it never appears in GET /posts/reviewed.
    #
    # Exposed to the client on PostRead, which is deliberate rather than an oversight.
    # A test post carries a visible marker so a reader has a fair chance of recognising
    # one, and hiding the flag while rendering the marker would be obscurity, not
    # secrecy. See app/core/trust.py: anomaly_ceiling for what stops that disclosure
    # from being exploitable, and CLAUDE.md for the part of it that remains.
    #
    # Indexed together with `created` because every query that touches it is "probes
    # older than X" (the pruning script) or "exclude probes from this history page".
    is_probe: Mapped[bool] = mapped_column(default=False, server_default="false")

    # The language this post is written in, and the other half of its routing key:
    # fan-out delivers it only to subscribers who accept that language (see
    # app/feed/keys.py: audience). One value, never a set - a post with two
    # languages would have to fan out into two audiences off one admission charge,
    # which is an economy change dressed as a data model.
    #
    # `LANGUAGE_UNSPECIFIED` ("und") means the post has no language at all - a photo,
    # a video, a caption-free meme - and routes through the whole channel instead of
    # one language's slice. Accepted only for a post with no text blocks (enforced in
    # api/posts.py:create_post), since that is the one case the claim is checkable.
    # Note the converse is *not* enforced: a text-free post may still declare a real
    # language, because a video can be spoken German.
    #
    # Self-declared and unverifiable - the server cannot read the post to check, which
    # is the whole reason detection lives in the client. The forward/drop economy is
    # what corrects a mislabelled post: it buys FEED_FANOUT deliveries to readers who
    # cannot read it, they drop it, and it goes no further.
    #
    # Deliberately no index. Nothing queries posts by language except `backfill_queue`
    # on a rebuild, which is already a full pass over the channel.
    language: Mapped[str] = mapped_column(
        String(8), default=LANGUAGE_UNSPECIFIED, server_default=LANGUAGE_UNSPECIFIED
    )

    forwarded_count: Mapped[int] = mapped_column(default=0, server_default="0")
    dropped_count: Mapped[int] = mapped_column(default=0, server_default="0")

    # Snapshot of the author's subscription at creation time (e.g. "supporter"),
    # or None for free/no perk. Set once, never updated — a supporter's posts keep
    # their look even after the subscription later lapses. See UserSubscription.
    subscription_kind: Mapped[str | None] = mapped_column(String(20), nullable=True)

    created: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    updated: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )

    channel: Mapped["Channel"] = relationship(back_populates="posts")
    author: Mapped["User | None"] = relationship(back_populates="posts")
    reviews: Mapped[list["PostReview"]] = relationship(
        back_populates="post", cascade="all, delete"
    )
    # Convenience/cascade-only collection, unordered - not what serialization reads
    # from (that's `blocks`, below). Kept mainly so deleting a Post still cascades
    # to every PostMedia row regardless of whether a block still references it.
    media: Mapped[list["PostMedia"]] = relationship(
        back_populates="post", cascade="all, delete"
    )
    # The post's actual content, in display order - see PostBlock's docstring.
    blocks: Mapped[list["PostBlock"]] = relationship(
        back_populates="post", cascade="all, delete", order_by="PostBlock.position"
    )
