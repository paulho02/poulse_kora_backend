import uuid
from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict


class PostBlockIn(BaseModel):
    """One block of a post being created - either a paragraph of text, or a
    reference to one of the files attached to the same multipart request. See
    create_post in app/api/posts.py for how `file_index` is validated/consumed."""

    type: Literal["text", "media"]
    text: str | None = None
    file_index: int | None = None  # index into the multipart `files` list
    # Videos only, and the single thing about a file the client gets to decide:
    # which of the two fixed shapes to center-crop the clip to
    # (app/core/media_validation.py). Images are cropped in the client and only
    # validated server-side, so this is ignored for them; omitted, a video falls
    # back to whichever allowed shape its source is closest to.
    orientation: Literal["landscape", "portrait"] | None = None


class PostCreate(BaseModel):
    channel_id: int
    blocks: list[PostBlockIn]
    is_anonymous: bool = False


class PostMediaRead(BaseModel):
    """One attachment's metadata - never its bytes; `url` (and `poster_url`) is
    where those live. See PostMedia in app/models/post_media.py.

    `width`/`height` let a client lay the block out before the bytes arrive.
    They are null only for rows predating the column, so treat missing dimensions
    as "unknown shape" rather than assuming one of the two fixed ratios.
    """

    id: int
    media_type: str
    content_type: str
    url: str
    duration_seconds: float | None
    width: int | None
    height: int | None
    # Video only: a still frame to show in place of the clip until playback
    # starts. Null for images (which are their own preview) and for a video whose
    # frame extraction failed.
    poster_url: str | None

    model_config = ConfigDict(from_attributes=True)


class PostBlockRead(BaseModel):
    type: str  # "text" | "media"
    text: str | None
    media: PostMediaRead | None

    model_config = ConfigDict(from_attributes=True)


class PostAuthor(BaseModel):
    id: uuid.UUID | None
    username: str | None
    # None both when the author has no picture set and when the post is anonymous -
    # see _serialize_post in app/api/posts.py, the single place that decides whether
    # to reveal the author at all.
    profile_picture_url: str | None

    model_config = ConfigDict(from_attributes=True)


class PostRead(BaseModel):
    id: int
    channel_id: int
    channel_name: str
    blocks: list[PostBlockRead]
    is_anonymous: bool
    author: PostAuthor
    # Deliberately no forwarded/dropped counts here. How a post has fared so far is
    # withheld until the viewer has committed to their own verdict - a reader who
    # can see that everyone else forwarded it is voting on the crowd, not on the
    # post, and the raw API would be the way around a client that merely hid it.
    # The numbers are returned once, by POST /posts/{id}/review - see
    # PostReviewResult in app/schemas/post_review.py.
    subscription_kind: str | None
    created: datetime

    model_config = ConfigDict(from_attributes=True)


class FeedEntry(BaseModel):
    """One slot in the review queue: the post that fills it, or a hole where a
    post used to be.

    The queue lives in Redis and holds nothing but post ids, so an author erasing
    their posts (see app/core/account_deletion.py) leaves ids behind in every
    reader's queue that still had one. Finding them would mean scanning every
    queue in the deployment; answering honestly costs nothing, because this route
    is already resolving those ids against Postgres.

    `post` is null for exactly that case. It is a separate envelope rather than a
    nullable field bolted onto `PostRead` because a vanished post genuinely has no
    channel, no author and no creation time - anything `PostRead` could carry for
    one would be invented. The client renders a ghost card the reader can dismiss
    (`DELETE /posts/feed/{post_id}`), and dismiss is all it can do: there is
    nothing left to forward.
    """

    post_id: int
    post: PostRead | None


class PostCreateResult(BaseModel):
    """Result of publishing an original post: the post plus what it cost.

    `price` is the dynamic admission cost charged against the token balance;
    `token_balance` is the balance after the spend (unchanged for superusers).
    """

    post: PostRead
    price: int
    token_balance: int


class PostEconomy(BaseModel):
    """The viewer's current posting economy — spendable tokens and the shared price
    to publish one original post. The price is a periodic snapshot (see
    app/feed/service.py: get_price_snapshot), not computed live, so it is the same
    for every viewer until `post_price_expires_at` — creating a post before then is
    charged this exact price."""

    token_balance: int
    post_price: int
    post_price_expires_at: datetime


class FeedStatus(BaseModel):
    """A cheap look at the review queue, for a client that wants to notice new
    arrivals without pulling the feed itself.

    `post_ids` is the whole queue in order, not a count: a client that keeps track
    of which ids it has already pulled can tell "something new arrived" from "the
    same posts, minus the ones I reviewed" exactly, and so never refetches for
    nothing. It is at most FEED_QUEUE_MAX_SLOTS entries, which is why returning all
    of them is cheaper than the count would suggest.

    `capacity` is that cap, and it is the difference between "nothing has been
    published for you yet" and "your queue is full — review something to make
    room", which are opposite instructions to give a reader.
    """

    post_ids: list[int]
    capacity: int
