from fastapi import APIRouter, HTTPException
from sqlalchemy import select
from sqlalchemy.orm import selectinload

from app.core.config import settings
from app.core.relay_rules import is_review_gate_unlocked
from app.deps.db import CurrentAsyncSession
from app.deps.users import CurrentUser
from app.models.channel import Channel
from app.models.channel_subscription import ChannelSubscription
from app.models.post import Post
from app.models.post_review import PostReview
from app.models.user import User
from app.schemas.post import PostAuthor, PostCreate, PostRead
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
    user: CurrentUser,
    channel_id: int | None = None,
    skip: int = 0,
    limit: int = 20,
):
    subscribed_ids = (
        (
            await session.execute(
                select(ChannelSubscription.channel_id).filter(
                    ChannelSubscription.user_id == user.id
                )
            )
        )
        .scalars()
        .all()
    )
    if not subscribed_ids:
        return []

    if channel_id is not None:
        if channel_id not in subscribed_ids:
            raise HTTPException(400, "Not subscribed to this channel")
        channel_ids = [channel_id]
    else:
        channel_ids = subscribed_ids

    already_reviewed = select(PostReview.post_id).filter(PostReview.user_id == user.id)

    query = (
        select(Post)
        .options(selectinload(Post.channel), selectinload(Post.author))
        .filter(
            Post.channel_id.in_(channel_ids),
            Post.author_id != user.id,
            Post.id.notin_(already_reviewed),
        )
        .order_by(Post.created.desc())
        .offset(skip)
        .limit(limit)
    )
    posts = (await session.execute(query)).scalars().all()
    return [_serialize_post(p, user) for p in posts]


@router.post("", response_model=PostRead, status_code=201)
async def create_post(
    post_in: PostCreate,
    session: CurrentAsyncSession,
    user: CurrentUser,
):
    channel = await session.get(Channel, post_in.channel_id)
    if not channel:
        raise HTTPException(404)

    # Posting to a channel no longer requires a subscription — subscriptions
    # only control what shows up in a user's feed. The review gate is the sole
    # gate on creating posts.
    if not is_review_gate_unlocked(user):
        raise HTTPException(
            403,
            {
                "error": "review_gate_locked",
                "reviewed_count": user.reviewed_count,
                "review_gate": settings.RELAY_REVIEW_GATE,
            },
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

    post = await _get_post_with_relations(session, post.id)
    return _serialize_post(post, user)


@router.get("/{post_id}", response_model=PostRead)
async def get_post(
    post_id: int,
    session: CurrentAsyncSession,
    user: CurrentUser,
):
    post = await _get_post_with_relations(session, post_id)
    if not post or not await _is_subscribed(session, user.id, post.channel_id):
        raise HTTPException(404)
    return _serialize_post(post, user)


@router.post("/{post_id}/review", response_model=PostReviewResult)
async def review_post(
    post_id: int,
    review_in: PostReviewCreate,
    session: CurrentAsyncSession,
    user: CurrentUser,
):
    post = await session.get(Post, post_id)
    if not post or not await _is_subscribed(session, user.id, post.channel_id):
        raise HTTPException(404)

    existing = await session.scalar(
        select(PostReview).filter(
            PostReview.user_id == user.id, PostReview.post_id == post_id
        )
    )
    if existing:
        raise HTTPException(409, {"error": "already_reviewed"})

    session.add(PostReview(user_id=user.id, post_id=post_id, kind=review_in.kind))
    user.reviewed_count += 1
    if review_in.kind == "forward":
        post.forwarded_count += 1
        user.forwarded_count += 1
    else:
        post.dropped_count += 1
        user.dropped_count += 1
    await session.commit()

    return PostReviewResult(
        post_id=post_id,
        kind=review_in.kind,
        reviewed_count=user.reviewed_count,
        review_gate=settings.RELAY_REVIEW_GATE,
        unlocked=is_review_gate_unlocked(user),
    )
