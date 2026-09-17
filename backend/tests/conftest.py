import asyncio
import uuid
from collections.abc import Callable

import pytest
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.core.config import settings
from app.core.languages import UNSPECIFIED
from app.db import Base
from app.deps.users import get_user_manager
from app.factory import create_app
from app.models.channel import Channel
from app.models.item import Item
from app.models.post import Post
from app.models.post_block import PostBlock
from app.models.post_media import PostMedia
from app.core.storage import post_media_key, storage
from app.models.user import User
from app.redis import redis_client
from tests.utils import generate_random_string

engine = create_async_engine(
    settings.ASYNC_DATABASE_URL,
)
async_session_maker = async_sessionmaker(
    engine,
    class_=AsyncSession,
    expire_on_commit=False,
    autocommit=False,
    autoflush=False,
)


@pytest.fixture(scope="session", autouse=True)
async def init_db():
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)


@pytest.fixture(scope="session", autouse=True)
async def init_storage():
    """Create the test bucket (TEST_S3_BUCKET_NAME, see app/core/config.py) and shut
    the storage client down at the end of the run.

    Needed because the tests drive the app through an ASGI transport, which never
    runs the lifespan that would otherwise create it. Objects written by a run are
    left behind: they are unreferenced once the test DB rolls back, and cleaning
    them up would only trade a harmless dev-bucket footprint for a slower, flakier
    teardown.
    """
    await storage.ensure_bucket()
    yield
    await storage.aclose()


@pytest.fixture(scope="session")
async def media_client():
    """A real network client, for fetching the presigned URLs the API hands out.

    The `client` fixture speaks ASGI directly to the app and so cannot reach the
    bucket at all - and that is the point of the change these tests cover: media
    bytes no longer come from this backend.
    """
    async with AsyncClient() as ac:
        yield ac


@pytest.fixture(scope="session")
async def db():
    async with async_session_maker() as session:
        yield session
        await session.rollback()
        await session.close()


@pytest.fixture(scope="session")
def default_password():
    return generate_random_string(32)


@pytest.fixture(scope="session")
def app():
    return create_app()


@pytest.fixture(scope="session")
async def client(app):
    async with AsyncClient(app=app, base_url="http://test") as ac:
        yield ac


@pytest.fixture(scope="function", autouse=True)
async def auto_rollback(db: AsyncSession):
    await db.rollback()


@pytest.fixture(scope="function", autouse=True)
def no_outbound_email(monkeypatch):
    """Pin the email connector to the one that cannot leave the machine.

    Unlike DATABASE_URL and REDIS_URL, EMAIL_PROVIDER has no TEST_ counterpart that
    config.py swaps in under pytest — it reads the same .env the dev app does. So a
    developer who sets EMAIL_PROVIDER=lettermint to work on it would, from then on,
    have every registration test in the suite fire a real API call at a real
    provider with real credentials, and the SMTP tests would silently stop testing
    SMTP. Anything that wants a specific connector overrides this itself; anything
    that doesn't gets the one whose unconfigured state is to log and return.
    """
    monkeypatch.setattr(settings, "EMAIL_PROVIDER", "smtp")
    monkeypatch.setattr(settings, "SMTP_HOST", None)


@pytest.fixture(scope="function", autouse=True)
async def flush_redis():
    """Isolate feed state per test. Runs against the test Redis DB (DB 1), which the
    config swaps in under pytest — never the dev DB."""
    await redis_client.flushdb()
    yield
    await redis_client.flushdb()


@pytest.fixture(scope="session")
def redis():
    """The shared async Redis client (test DB 1), for tests that drive feed state
    directly rather than through the API."""
    return redis_client


@pytest.fixture(scope="session")
def create_user(db: AsyncSession, default_password: str):
    user_manager = next(get_user_manager())

    async def inner(is_verified: bool = True):
        # Defaults to verified: most tests exercise feed/channel/item behavior and
        # shouldn't have to know about email verification to get a working user.
        # Pass is_verified=False for tests that specifically exercise the
        # REQUIRE_EMAIL_VERIFICATION gate (see tests/api/test_email_verification.py).
        user = User(
            id=uuid.uuid4(),
            email=f"{generate_random_string(20)}@{generate_random_string(10)}.com",
            hashed_password=user_manager.password_helper.hash(default_password),
            is_verified=is_verified,
        )
        db.add(user)
        await db.commit()
        return user

    return inner


@pytest.fixture(scope="session")
def create_item(db: AsyncSession, create_user: Callable):
    async def inner(user=None):
        if not user:
            user = await create_user()
        item = Item(
            user=user,
            value="value",
        )
        db.add(item)
        await db.commit()
        return item

    return inner


@pytest.fixture(scope="session")
def create_channel(db: AsyncSession):
    async def inner(name=None, color="#000000", description="desc"):
        channel = Channel(
            name=name or generate_random_string(20),
            color=color,
            description=description,
        )
        db.add(channel)
        await db.commit()
        return channel

    return inner


@pytest.fixture(scope="session")
def create_post(db: AsyncSession, create_user: Callable, create_channel: Callable):
    async def inner(
        channel=None,
        author=None,
        text="text",
        is_anonymous=False,
        media: list[dict] | None = None,
        language=UNSPECIFIED,
    ):
        """`text`, if non-empty, becomes a single leading text block - matching
        every real post's shape (text block(s) then media, see PostBlock).
        `language` defaults to UNSPECIFIED so a post made by this factory routes
        through the whole channel - the pre-language behaviour, which is what the
        tests that predate routing assume. Tests about routing pass a real language.

        `media`, if given, is a list of kwargs for PostMedia (media_type,
        content_type, size_bytes, duration_seconds); each becomes a media block
        after the text block, inserted directly and bypassing upload validation,
        since tests exercising post-visibility (rather than the upload path
        itself) don't need real image/video bytes.

        A `data` key is not a column - it is bytes to actually put in the bucket,
        replaced here by the `object_key` they landed under. Real bytes matter
        because a media URL is now a presigned link the test fetches from the
        bucket itself, so there has to be something behind it.
        """
        if not channel:
            channel = await create_channel()
        if not author:
            author = await create_user()
        post = Post(
            channel_id=channel.id,
            author_id=author.id,
            is_anonymous=is_anonymous,
            language=language,
        )
        db.add(post)
        await db.flush()

        position = 0
        if text:
            db.add(
                PostBlock(
                    post_id=post.id, position=position, block_type="text", text=text
                )
            )
            position += 1
        for item in media or []:
            item = dict(item)
            data = item.pop("data", None)
            if data is not None:
                key = post_media_key(item.get("content_type", "image/jpeg"))
                await storage.put_object(
                    key, data, content_type=item.get("content_type", "image/jpeg")
                )
                item["object_key"] = key
            m = PostMedia(post_id=post.id, **item)
            db.add(m)
            await db.flush()
            db.add(
                PostBlock(
                    post_id=post.id,
                    position=position,
                    block_type="media",
                    media_id=m.id,
                )
            )
            position += 1

        await db.commit()
        # Pre-load so callers can read post.media/post.blocks synchronously
        # afterwards, without triggering a lazy-load outside the async session
        # context.
        await db.refresh(post, attribute_names=["media", "blocks"])
        return post

    return inner


@pytest.fixture(scope="session")
def event_loop():
    loop = asyncio.get_event_loop()
    yield loop
    loop.close()
