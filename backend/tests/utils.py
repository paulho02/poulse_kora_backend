import io
import secrets
import string
from typing import Any

from PIL import Image

from fastapi_users.jwt import generate_jwt
from sqlalchemy.ext.asyncio import AsyncSession

from app.deps.users import get_jwt_strategy
from app.models.channel import Channel
from app.models.channel_subscription import ChannelSubscription
from app.models.post import Post
from app.models.post_review import PostReview
from app.models.user import User
from app.models.user_subscription import UserSubscription


def generate_random_string(length: int) -> str:
    return "".join(secrets.choice(string.ascii_lowercase) for i in range(length))


def get_jwt_header(user: User) -> Any:
    jwt_strategy = get_jwt_strategy()
    data = {"sub": str(user.id), "aud": jwt_strategy.token_audience}
    token = generate_jwt(data, jwt_strategy.secret, jwt_strategy.lifetime_seconds)
    return {"Authorization": f"Bearer {token}"}


async def subscribe(db: AsyncSession, user: User, channel: Channel) -> ChannelSubscription:
    subscription = ChannelSubscription(user_id=user.id, channel_id=channel.id)
    db.add(subscription)
    await db.commit()
    return subscription


async def grant_subscription(db: AsyncSession, user: User, kind: str) -> UserSubscription:
    subscription = UserSubscription(user_id=user.id, kind=kind)
    db.add(subscription)
    await db.commit()
    return subscription


async def review(db: AsyncSession, user: User, post: Post, kind: str) -> PostReview:
    post_review = PostReview(user_id=user.id, post_id=post.id, kind=kind)
    db.add(post_review)
    user.reviewed_count += 1
    if kind == "forward":
        post.forwarded_count += 1
        user.forwarded_count += 1
    else:
        post.dropped_count += 1
        user.dropped_count += 1
    await db.commit()
    return post_review


def make_test_png(
    width: int = 64,
    height: int = 64,
    *,
    color: tuple[int, int, int] = (200, 50, 50),
    with_exif: bool = False,
) -> bytes:
    """A real, decodable PNG - which profile-picture uploads now require.

    Placeholder bytes used to be good enough here, because the route stored
    whatever it was handed under whatever type the client claimed. It validates
    and re-encodes now (app/core/media_validation.py: process_profile_picture),
    so a test that wants an upload to succeed has to send an actual image.
    """
    img = Image.new("RGB", (width, height), color=color)
    buf = io.BytesIO()
    if with_exif:
        exif = Image.Exif()
        exif[0x0112] = 1  # Orientation, standing in for the GPS tags that matter.
        img.save(buf, format="PNG", exif=exif)
    else:
        img.save(buf, format="PNG")
    return buf.getvalue()
