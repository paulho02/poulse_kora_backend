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
    forwarded_count: int
    dropped_count: int
    subscription_kind: str | None
    created: datetime

    model_config = ConfigDict(from_attributes=True)


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
