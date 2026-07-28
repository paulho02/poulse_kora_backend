from datetime import datetime, timezone

from fastapi import APIRouter, Depends
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import selectinload

from app.core.config import settings
from app.core.errors import api_error
from app.core.relay_rules import is_review_gate_unlocked
from app.deps.db import CurrentAsyncSession
from app.deps.rate_limit import limit_interactions
from app.deps.redis import CurrentRedis
from app.deps.users import CurrentVerifiedUser
from app.feed import service
from app.models.channel import Channel
from app.models.channel_subscription import ChannelSubscription
from app.models.post import Post
from app.models.post_review import PostReview
from app.models.user import User
from app.schemas.post import (
    PostAuthor,
    PostCreate,
    PostCreateResult,
    PostEconomy,
    PostRead,
)
from app.schemas.post_review import PostReviewCreate, PostReviewResult

router = APIRouter(prefix="/posts")


def _serialize_post(post: Post, viewer: User) -> PostRead:
    reveal_author = (
        not post.is_anonymous
        or post.author_id == viewer.id
        or viewer.is_superuser
    )
    author = (
        PostAuthor(id=post.author_id, username=post.author.username)
        if reveal_author
        else PostAuthor(id=None, username=None)
    )
    return PostRead(
        id=post.id,
        channel_id=post.channel_id,
        channel_name=post.channel.name,
        text=post.text,
        has_image=post.has_image,
        is_anonymous=post.is_anonymous,
        author=author,
        forwarded_count=post.forwarded_count,
        dropped_count=post.dropped_count,
        created=post.created,
    )


async def _is_subscribed(session: CurrentAsyncSession, user_id, channel_id: int) -> bool:
    return (
        await session.scalar(
            select(ChannelSubscription).filter(
                ChannelSubscription.user_id == user_id,
                ChannelSubscription.channel_id == channel_id,
            )
        )
    ) is not None


async def _get_post_with_relations(session: CurrentAsyncSession, post_id: int) -> Post | None:
    return await session.scalar(
        select(Post)
        .options(selectinload(Post.channel), selectinload(Post.author))
        .filter(Post.id == post_id)
    )


@router.get("/feed", response_model=list[PostRead])
async def get_posts_feed(
    session: CurrentAsyncSession,
    user: CurrentVerifiedUser,
    redis: CurrentRedis,
    channel_id: int | None = None,
    skip: int = 0,
    limit: int = 20,
):
    """Return the user's review queue, oldest first, rendered from Postgres by ID.

    The queue is maintained in Redis by the distribution worker; here we just read
    the post_ids and hydrate them. `place_post` dedupes on insert, so a post appears
    at most once in the queue even when fan-out and backfill both deliver it.
    """
    post_ids = await service.render_queue_ids(redis, str(user.id), limit, skip)
    if not post_ids:
        return []

    posts = (
        (
            await session.execute(
                select(Post)
                .options(selectinload(Post.channel), selectinload(Post.author))
                .filter(Post.id.in_(post_ids))
            )
        )
        .scalars()
        .all()
    )
    by_id = {p.id: p for p in posts}
    ordered = [by_id[pid] for pid in post_ids if pid in by_id]
    if channel_id is not None:
        ordered = [p for p in ordered if p.channel_id == channel_id]
    return [_serialize_post(p, user) for p in ordered]


@router.post(
    "",
    response_model=PostCreateResult,
    status_code=201,
    dependencies=[Depends(limit_interactions)],
)
async def create_post(
    post_in: PostCreate,
    session: CurrentAsyncSession,
    user: CurrentVerifiedUser,
    redis: CurrentRedis,
):
    """Publish an original post. Costs a dynamic number of tokens (admission price)
    that rises with operation-queue congestion; superusers post for free. The post
    is enqueued as an operation for the worker to distribute.

    The price charged is the current shared snapshot (see
    service.get_price_snapshot), not a fresh live computation — the same number a
    concurrent `GET /posts/economy` would have quoted, rather than one that could have
    drifted in the seconds between the two calls.

    Shares the per-user interaction budget with reviewing (see app/deps/rate_limit.py),
    so a burst of posts and forwards together still can't flood the queue."""
    channel = await session.get(Channel, post_in.channel_id)
    if not channel:
        raise api_error(404, "channel_not_found")

    price = (await service.get_price_snapshot(redis))["price"]
    if user.is_superuser:
        token_balance = await service.token_balance(redis, str(user.id))
    else:
        token_balance = await service.spend_tokens(redis, str(user.id), price)
        if token_balance is None:
            raise api_error(
                402,
                "insufficient_tokens",
                balance=await service.token_balance(redis, str(user.id)),
                price=price,
            )

    post = Post(
        channel_id=post_in.channel_id,
        author_id=user.id,
        text=post_in.text,
        has_image=post_in.has_image,
        is_anonymous=post_in.is_anonymous,
    )
    session.add(post)
    await session.commit()

    await service.enqueue_operation(
        redis, post.id, post.channel_id, author_id=str(post.author_id)
    )

    post = await _get_post_with_relations(session, post.id)
    return PostCreateResult(
        post=_serialize_post(post, user),
        price=price,
        token_balance=token_balance,
    )


@router.get("/economy", response_model=PostEconomy)
async def get_post_economy(user: CurrentVerifiedUser, redis: CurrentRedis):
    """Current spendable token balance and the shared price to publish a post, plus
    the instant that price stops being guaranteed (see service.get_price_snapshot).

    Declared before `/{post_id}` so the literal path wins the route match.
    """
    snapshot = await service.get_price_snapshot(redis)
    balance = await service.token_balance(redis, str(user.id))
    return PostEconomy(
        token_balance=balance,
        post_price=snapshot["price"],
        post_price_expires_at=datetime.fromtimestamp(
            snapshot["expires_at"], tz=timezone.utc
        ),
    )


@router.get("/{post_id}", response_model=PostRead)
async def get_post(
    post_id: int,
    session: CurrentAsyncSession,
    user: CurrentVerifiedUser,
):
    post = await _get_post_with_relations(session, post_id)
    if not post or not await _is_subscribed(session, user.id, post.channel_id):
        raise api_error(404, "post_not_found")
    return _serialize_post(post, user)


@router.post(
    "/{post_id}/review",
    response_model=PostReviewResult,
    dependencies=[Depends(limit_interactions)],
)
async def review_post(
    post_id: int,
    review_in: PostReviewCreate,
    session: CurrentAsyncSession,
    user: CurrentVerifiedUser,
    redis: CurrentRedis,
):
    """Forward or drop a post from the user's queue.

    Removing the post from the Redis queue is the concurrency guard (a post can only
    be reviewed while it sits in the queue). Reviewing earns one token; forwarding
    re-injects the post as a new operation so it propagates to more users.

    Rate-limited per user (see app/deps/rate_limit.py): swiping faster than the limit
    isn't reading, and a token is earned per review, so the cap also stops someone
    farming tokens by machine-gunning drops.
    """
    post = await session.get(Post, post_id)
    if not post:
        raise api_error(404, "post_not_found")

    removed = await service.claim_from_queue(redis, str(user.id), post_id)
    if removed == 0:
        raise api_error(409, "not_in_queue")

    session.add(PostReview(user_id=user.id, post_id=post_id, kind=review_in.kind))
    user.reviewed_count += 1
    if review_in.kind == "forward":
        post.forwarded_count += 1
        user.forwarded_count += 1
    else:
        post.dropped_count += 1
        user.dropped_count += 1
    try:
        await session.commit()
    except IntegrityError:
        # Re-delivery: a fan-out (e.g. a due ops:retry) put back a post this user had
        # already reviewed. It has been removed from the queue above; nothing to record.
        await session.rollback()
        raise api_error(409, "already_reviewed") from None

    token_balance = await service.earn_token(redis, str(user.id))

    if review_in.kind == "forward":
        # The author travels with the post, not with whoever forwarded it — a forward
        # must still never land back on the person who wrote it.
        await service.enqueue_operation(
            redis, post_id, post.channel_id, author_id=str(post.author_id)
        )

    return PostReviewResult(
        post_id=post_id,
        kind=review_in.kind,
        reviewed_count=user.reviewed_count,
        review_gate=settings.RELAY_REVIEW_GATE,
        unlocked=is_review_gate_unlocked(user),
        token_balance=token_balance,
    )
