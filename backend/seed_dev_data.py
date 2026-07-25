"""Seed the dev database with bot-authored posts so the Relay review flow
(feed / forward / drop) can actually be exercised.

`get_posts_feed` (app/api/posts.py) excludes posts authored by the viewer, so a
single real dev account never sees anything in its own feed until *someone else*
has posted. This script creates a handful of fixed "bot" users and tops up every
channel with a few realistic posts from them, so any real account just needs to
subscribe to a channel (via the app) to have something to review.

Safe to re-run: bot users are matched by email, and each channel is only topped
up to TARGET_POSTS_PER_CHANNEL bot-authored posts, so re-running won't pile up
duplicates.

Usage (inside the backend container):
    docker compose exec backend python scripts/seed_dev_data.py
"""

import asyncio
import random
import uuid

from sqlalchemy import func, select

from app.db import async_session_maker
from app.deps.users import get_user_manager
from app.feed.service import rebuild_from_pg
from app.models.channel import Channel
from app.models.post import Post
from app.models.user import User
from app.redis import redis_client

TARGET_POSTS_PER_CHANNEL = 6
BOT_PASSWORD = "devpassword123"  # dev-only seed accounts, not meant to be logged into

BOTS = [
    {"email": "relay.bot.ada@kora.dev", "username": "ada_relay"},
    {"email": "relay.bot.grace@kora.dev", "username": "grace_relay"},
    {"email": "relay.bot.linus@kora.dev", "username": "linus_relay"},
    {"email": "relay.bot.marie@kora.dev", "username": "marie_relay"},
]

# (text, is_anonymous) — exactly TARGET_POSTS_PER_CHANNEL entries per known channel
# so idempotent top-ups can slice in deterministically without duplicating text.
CHANNEL_POSTS = {
    "General": [
        ("Just found out our office coffee machine has a secret decaf setting. Mind blown.", False),
        ("What's everyone's go-to productivity hack this week?", False),
        ("PSA: daylight savings ends this weekend, adjust your alarms.", True),
        ("Anyone else's Monday feel like it's already Wednesday?", False),
        ("Trying a new note-taking system. Three days in, cautiously optimistic.", True),
        ("Rate my desk setup: standing desk, one monitor, too many sticky notes.", False),
    ],
    "Technology": [
        ("New JS framework just dropped and it's already deprecated. Classic.", False),
        ("Finally switched to a mechanical keyboard. My coworkers hate me now.", False),
        ("Is anyone actually using the new AI code review tools in production?", True),
        ("Self-hosted my first server this weekend. Never going back to cloud for side projects.", False),
        ("Unpopular opinion: dark mode isn't actually better for your eyes.", True),
        ("Just automated a task that used to take 2 hours a week down to 30 seconds.", False),
    ],
    "Outdoors": [
        ("Trail conditions on the ridge loop are great right now, highly recommend.", False),
        ("Finally upgraded my tent after 8 years. Should've done it sooner.", False),
        ("Saw a family of deer on this morning's hike, made my whole week.", True),
        ("Anyone have recommendations for a beginner-friendly multi-day backpacking route?", False),
        ("Packed way too much food for a day hike again. No regrets.", True),
        ("Weather looks perfect for camping this weekend, who's in?", False),
    ],
    "Memes": [
        ("me: I'll just check one notification / also me: 45 minutes later still scrolling", False),
        ("that feeling when your code works and you don't know why", True),
        ("nobody: / my brain at 3am: remember that embarrassing thing from 2014", False),
        ("when the meeting could've been an email but here we are anyway", False),
        ("me pretending to understand the meeting while frantically googling under the table", True),
        ("plot twist: the bug was a missing semicolon the whole time", False),
    ],
    "Politics": [
        ("Local council meeting on the new zoning proposal is next Tuesday, worth attending.", False),
        ("Curious how others are reading the latest policy proposal, keeping this civil please.", True),
        ("Turnout for the town hall last night was way higher than expected.", False),
        ("Does anyone have a good breakdown of the ballot measures this cycle?", False),
        ("Reminder that voter registration deadlines are coming up soon.", True),
        ("Interesting to see how the debate shifted after the second round of questions.", False),
    ],
    "Local": [
        ("New coffee shop on Main Street is worth the hype, try the oat milk latte.", False),
        ("Farmers market is back this Saturday, first one of the season.", False),
        ("Road construction on 5th is causing a mess during rush hour, plan accordingly.", True),
        ("Looking for recommendations for a reliable plumber in the area.", False),
        ("The park cleanup this weekend had a great turnout, thanks to everyone who showed up.", True),
        ("Anyone know what's opening in the old bookstore spot downtown?", False),
    ],
}

# Fallback for any channel created after this script was written.
GENERIC_POSTS = [
    ("Excited to see this channel getting some activity, what's everyone working on?", False),
    ("First post here, hope this is useful to the community.", True),
    ("Sharing this because I think it's worth a discussion.", False),
    ("Curious what people think about this one.", False),
    ("Dropping this here for visibility.", True),
    ("Anyone else notice this recently?", False),
]


async def _ensure_bots(session, user_manager) -> list[User]:
    bots = []
    for spec in BOTS:
        existing = await session.scalar(select(User).filter(User.email == spec["email"]))
        if existing:
            bots.append(existing)
            continue
        bot = User(
            id=uuid.uuid4(),
            email=spec["email"],
            username=spec["username"],
            hashed_password=user_manager.password_helper.hash(BOT_PASSWORD),
            is_active=True,
            is_verified=True,
        )
        session.add(bot)
        bots.append(bot)
    await session.commit()
    return bots


async def _top_up_channel(session, channel: Channel, bots: list[User]) -> int:
    bot_ids = [b.id for b in bots]
    existing_count = await session.scalar(
        select(func.count())
        .select_from(Post)
        .filter(Post.channel_id == channel.id, Post.author_id.in_(bot_ids))
    )

    pool = CHANNEL_POSTS.get(channel.name, GENERIC_POSTS)
    to_add = pool[existing_count:TARGET_POSTS_PER_CHANNEL]

    for text, is_anonymous in to_add:
        session.add(
            Post(
                channel_id=channel.id,
                author_id=random.choice(bots).id,
                text=text,
                is_anonymous=is_anonymous,
                has_image=False,
            )
        )
    return len(to_add)


async def main():
    async with async_session_maker() as session:
        user_manager = next(get_user_manager())
        bots = await _ensure_bots(session, user_manager)

        channels = (await session.execute(select(Channel))).scalars().all()
        if not channels:
            print("No channels found — run `alembic upgrade head` first.")
            return

        total_created = 0
        for channel in channels:
            created = await _top_up_channel(session, channel, bots)
            total_created += created
            print(f"{channel.name}: +{created} posts")

        await session.commit()

        # Reconcile Redis distribution state (channel sets, free_queue, token
        # balances) with the freshly-seeded Postgres data. Subscribing to a channel
        # (via the app) then backfills the queue with these posts to review.
        redis_stats = await rebuild_from_pg(redis_client, session)

        print(
            f"Done. {len(bots)} bot users, {total_created} new posts. "
            f"Redis: {redis_stats['subscriptions']} subscriptions, "
            f"{redis_stats['users']} users seeded, "
            f"{redis_stats['backfilled']} posts backfilled into queues."
        )


if __name__ == "__main__":
    asyncio.run(main())
