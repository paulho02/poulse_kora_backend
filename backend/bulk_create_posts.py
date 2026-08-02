"""Bulk-create posts for testing/debugging, by calling the real `create_post`
route function directly (same pricing, subscription, and enqueue logic a real
`POST /posts` request would run) — so this script always reflects actual post
creation behavior instead of re-implementing it.

Posts are authored by a dedicated superuser bot account (auto-created on first
use), so bulk runs never get blocked on token balance / post price the way a
regular user would.

Usage (inside the backend container):
    docker compose exec backend python bulk_create_posts.py <channel> <amount>

<channel> is a channel ID (e.g. 3) or exact channel name (e.g. General).

Example:
    docker compose exec backend python bulk_create_posts.py General 20
"""

import argparse
import asyncio
import uuid
from datetime import datetime, timezone

from sqlalchemy import select

from app.api.posts import create_post
from app.db import async_session_maker
from app.deps.users import get_user_manager
from app.models.channel import Channel
from app.models.user import User
from app.redis import redis_client
from app.schemas.post import PostCreate

BOT_EMAIL = "bulk.post.bot@kora.dev"
BOT_USERNAME = "bulk_post_bot"
BOT_PASSWORD = "devpassword123"  # dev-only seed account, not meant to be logged into


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("channel", help="Channel ID or exact channel name")
    parser.add_argument("amount", type=int, help="Number of posts to create")
    return parser.parse_args()


async def _resolve_channel(session, channel_ref: str) -> Channel:
    channel = None
    if channel_ref.isdigit():
        channel = await session.get(Channel, int(channel_ref))
    else:
        channel = await session.scalar(select(Channel).filter(Channel.name == channel_ref))
    if not channel:
        raise SystemExit(f"Channel not found: {channel_ref!r}")
    return channel


async def _ensure_bot_author(session, user_manager) -> User:
    existing = await session.scalar(select(User).filter(User.email == BOT_EMAIL))
    if existing:
        return existing
    bot = User(
        id=uuid.uuid4(),
        email=BOT_EMAIL,
        username=BOT_USERNAME,
        hashed_password=user_manager.password_helper.hash(BOT_PASSWORD),
        is_active=True,
        is_verified=True,
        is_superuser=True,
    )
    session.add(bot)
    await session.commit()
    await session.refresh(bot)
    return bot


async def main():
    args = parse_args()
    if args.amount <= 0:
        raise SystemExit("amount must be a positive integer")

    async with async_session_maker() as session:
        channel = await _resolve_channel(session, args.channel)
        user_manager = next(get_user_manager())
        author = await _ensure_bot_author(session, user_manager)

        run_stamp = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
        for i in range(1, args.amount + 1):
            post_in = PostCreate(
                channel_id=channel.id,
                text=f"This post is auto generated. {run_stamp}-{i}",
            )
            result = await create_post(post_in, session, author, redis_client)
            print(f"[{i}/{args.amount}] created post id={result.post.id}")

        print(
            f"Done. Created {args.amount} posts in channel "
            f"'{channel.name}' (id={channel.id}) as {author.username}."
        )


if __name__ == "__main__":
    asyncio.run(main())
