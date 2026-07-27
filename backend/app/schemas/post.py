import uuid
from datetime import datetime

from pydantic import BaseModel, ConfigDict


class PostCreate(BaseModel):
    channel_id: int
    text: str
    has_image: bool = False
    is_anonymous: bool = False


class PostAuthor(BaseModel):
    id: uuid.UUID | None
    username: str | None

    model_config = ConfigDict(from_attributes=True)


class PostRead(BaseModel):
    id: int
    channel_id: int
    channel_name: str
    text: str
    has_image: bool
    is_anonymous: bool
    author: PostAuthor
    forwarded_count: int
    dropped_count: int
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
