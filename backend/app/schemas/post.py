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
