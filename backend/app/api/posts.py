from datetime import datetime, timezone

from fastapi import APIRouter, Depends, File, Form, UploadFile
from pydantic import TypeAdapter, ValidationError
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import selectinload

from app.core.config import settings
from app.core.errors import api_error
from app.core.logger import logger
from app.core.media_validation import ProcessedMedia, process_upload
from app.core.relay_rules import is_review_gate_unlocked
from app.core.storage import StorageError, post_media_key, storage
from app.deps.db import CurrentAsyncSession
from app.deps.rate_limit import limit_interactions
from app.deps.redis import CurrentRedis
from app.deps.users import CurrentVerifiedUser
from app.feed import service
from app.models.channel import Channel
from app.models.post import Post
from app.models.post_block import PostBlock
from app.models.post_media import PostMedia
from app.models.post_review import PostReview
from app.models.user import User
from app.models.user_subscription import UserSubscription
from app.schemas.post import (
    PostAuthor,
    PostBlockIn,
    PostBlockRead,
    PostCreate,
    PostCreateResult,
    PostEconomy,
    PostMediaRead,
    PostRead,
)
from app.schemas.post_review import PostReviewCreate, PostReviewResult, ReviewedPostRead

router = APIRouter(prefix="/posts")

_blocks_adapter = TypeAdapter(list[PostBlockIn])

# Stored on every media object and echoed back by the bucket on a presigned GET.
# Safe to make aggressive: an object key is a fresh UUID per upload and nothing
# ever rewrites one, so a cached response can never go stale - the object is
# either the one the URL names or gone. `private` because the URL is a
# capability, and shared caches have no business keeping a copy of it.
_MEDIA_CACHE_CONTROL = "private, max-age=86400, immutable"


async def post_create_form(
    channel_id: int = Form(...),
    blocks: str = Form(...),
    is_anonymous: bool = Form(False),
) -> PostCreate:
    """`POST /posts` is multipart (it accepts files), so its non-file fields arrive
    as form fields rather than a JSON body. `blocks` is itself a JSON-encoded list
    (multipart has no native way to carry a nested list of objects) - parsed here
    into the same `PostBlockIn` shape `create_post` validates further.
    """
    try:
        parsed_blocks = _blocks_adapter.validate_json(blocks)
    except ValidationError:
        raise api_error(400, "post_blocks_invalid") from None
    return PostCreate(channel_id=channel_id, blocks=parsed_blocks, is_anonymous=is_anonymous)


def _serialize_post(post: Post, viewer: User) -> PostRead:
    reveal_author = (
        not post.is_anonymous
        or post.author_id == viewer.id
        or viewer.is_superuser
    )
    author = (
        PostAuthor(
            id=post.author_id,
            username=post.author.username,
            profile_picture_url=post.author.profile_picture_url,
        )
        if reveal_author
        else PostAuthor(id=None, username=None, profile_picture_url=None)
    )
    return PostRead(
        id=post.id,
        channel_id=post.channel_id,
        channel_name=post.channel.name,
        blocks=[
            PostBlockRead(
                type=b.block_type,
                text=b.text,
                media=PostMediaRead.model_validate(b.media) if b.media else None,
            )
            for b in post.blocks
        ],
        is_anonymous=post.is_anonymous,
        author=author,
        forwarded_count=post.forwarded_count,
        dropped_count=post.dropped_count,
        subscription_kind=post.subscription_kind,
        created=post.created,
    )


async def _can_view_post(
    session: CurrentAsyncSession, redis: CurrentRedis, user: User, post: Post
) -> bool:
    """A post is only visible to its author or someone it was actually delivered to
    (currently in their queue, or already reviewed by them) — channel subscription
    alone is not enough, since fan-out/exclusions mean a subscriber may never have
    had this particular post placed in their feed.
    """
    if user.is_superuser or post.author_id == user.id:
        return True
    if await service.is_queued(redis, str(user.id), post.id):
        return True
    return (
        await session.scalar(
            select(PostReview).filter(
                PostReview.user_id == user.id, PostReview.post_id == post.id
            )
        )
    ) is not None


async def _store_media(
    processed_by_index: dict[int, ProcessedMedia],
) -> dict[int, tuple[str, str | None]]:
    """Upload every processed attachment (and its poster frame) to the media
    bucket, returning `(object_key, poster_object_key)` per file index.

    All-or-nothing as far as the caller is concerned: the first failure deletes
    whatever this call has already written and raises, so a post never half-lands
    with some objects live and others missing. Nothing has touched Postgres yet at
    this point, so there is no row to unwind alongside it.
    """
    written: list[str] = []
    keys: dict[int, tuple[str, str | None]] = {}
    try:
        for index, item in processed_by_index.items():
            key = post_media_key(item.content_type)
            await storage.put_object(
                key,
                item.data,
                content_type=item.content_type,
                cache_control=_MEDIA_CACHE_CONTROL,
            )
            written.append(key)

            poster_key = None
            if item.poster and item.poster_content_type:
                poster_key = post_media_key(item.poster_content_type)
                await storage.put_object(
                    poster_key,
                    item.poster,
                    content_type=item.poster_content_type,
                    cache_control=_MEDIA_CACHE_CONTROL,
                )
                written.append(poster_key)
            keys[index] = (key, poster_key)
    except StorageError:
        logger.exception(
            "post media upload failed, discarding %d already-written object(s)",
            len(written),
        )
        for key in written:
            await storage.delete_object(key)
        raise api_error(503, "media_storage_unavailable") from None
    return keys


async def _get_post_with_relations(session: CurrentAsyncSession, post_id: int) -> Post | None:
    return await session.scalar(
        select(Post)
        .options(
            selectinload(Post.channel),
            selectinload(Post.author),
            selectinload(Post.blocks).selectinload(PostBlock.media),
        )
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
                .options(
                    selectinload(Post.channel),
                    selectinload(Post.author),
                    selectinload(Post.blocks).selectinload(PostBlock.media),
                )
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
    session: CurrentAsyncSession,
    user: CurrentVerifiedUser,
    redis: CurrentRedis,
    post_in: PostCreate = Depends(post_create_form),
    files: list[UploadFile] = File(default=[]),
):
    """Publish an original post: an ordered sequence of text and media blocks (see
    `PostBlock`), article-style, up to POST_MEDIA_MAX_FILES media blocks total.
    Costs a dynamic number of tokens (admission price) that rises with
    operation-queue congestion; superusers post for free. The post is enqueued as
    an operation for the worker to distribute.

    Multipart, in one request/one transaction, rather than a "create post, then
    attach media" two-step: the post is enqueued to Redis fan-out immediately on
    creation (`service.enqueue_operation`), so a two-step flow would let a
    recipient's `GET /posts/feed` render the post before a second call had attached
    its media - and would leave a genuine partial-failure state (post live, upload
    failed) with no obvious retry story.

    `blocks` is a JSON-encoded list (parsed in `post_create_form`); each media block
    names a `file_index` into the parallel `files` list rather than carrying bytes
    itself. Every file is validated/re-encoded (app/core/media_validation.py) - and
    any resulting error raised - before the channel-existence check's price is
    charged, so a bad upload never costs tokens.

    The price charged is the current shared snapshot (see
    service.get_price_snapshot), not a fresh live computation — the same number a
    concurrent `GET /posts/economy` would have quoted, rather than one that could have
    drifted in the seconds between the two calls.

    Shares the per-user interaction budget with reviewing (see app/deps/rate_limit.py),
    so a burst of posts and forwards together still can't flood the queue."""
    channel = await session.get(Channel, post_in.channel_id)
    if not channel:
        raise api_error(404, "channel_not_found")

    blocks = post_in.blocks
    if not blocks:
        raise api_error(400, "post_blocks_empty")
    if len(blocks) > settings.POST_BLOCKS_MAX_COUNT:
        raise api_error(400, "post_blocks_too_many")

    media_blocks = [b for b in blocks if b.type == "media"]
    if len(media_blocks) > settings.POST_MEDIA_MAX_FILES:
        raise api_error(400, "post_media_too_many_files")

    used_file_indices: set[int] = set()
    for block in blocks:
        if block.type == "text":
            if not block.text or not block.text.strip():
                raise api_error(400, "post_blocks_invalid")
        else:
            if block.file_index is None or not (0 <= block.file_index < len(files)):
                raise api_error(400, "post_blocks_invalid")
            if block.file_index in used_file_indices:
                raise api_error(400, "post_blocks_invalid")
            used_file_indices.add(block.file_index)
    if len(used_file_indices) != len(files):
        # An uploaded file nobody's block points at - reject rather than silently
        # dropping it (it was still charged bandwidth/validation cost for nothing).
        raise api_error(400, "post_blocks_invalid")

    # Only a video block's `orientation` is honoured (see PostBlockIn); the map is
    # keyed by file index because that, not block position, is what identifies a
    # file in the parallel `files` list.
    orientation_by_index = {
        block.file_index: block.orientation for block in media_blocks
    }

    processed_by_index: dict[int, ProcessedMedia] = {}
    total_media_bytes = 0
    for index, file in enumerate(files):
        item = await process_upload(file, orientation_by_index.get(index))
        total_media_bytes += item.size_bytes
        if total_media_bytes > settings.POST_MEDIA_MAX_TOTAL_BYTES:
            raise api_error(400, "post_media_total_too_large")
        processed_by_index[index] = item

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

    is_supporter = (
        await session.scalar(
            select(UserSubscription).where(
                UserSubscription.user_id == user.id,
                UserSubscription.kind == "supporter",
            )
        )
    ) is not None

    # Bytes go to the bucket before anything is committed, so a storage failure
    # aborts the post rather than committing rows that point at objects which do
    # not exist. The reverse ordering is not available to us - the transaction can
    # still roll back afterwards, and then these objects are orphaned. Orphaned
    # bytes nobody references are the cheap failure; a media block that 404s in
    # every recipient's feed is not.
    stored_by_index = await _store_media(processed_by_index)

    post = Post(
        channel_id=post_in.channel_id,
        author_id=user.id,
        is_anonymous=post_in.is_anonymous,
        subscription_kind="supporter" if is_supporter else None,
    )
    session.add(post)
    await session.flush()  # need post.id for the block/media rows below
    for position, block in enumerate(blocks):
        if block.type == "media":
            item = processed_by_index[block.file_index]
            object_key, poster_object_key = stored_by_index[block.file_index]
            media = PostMedia(
                post_id=post.id,
                media_type=item.media_type,
                content_type=item.content_type,
                object_key=object_key,
                size_bytes=item.size_bytes,
                duration_seconds=item.duration_seconds,
                width=item.width,
                height=item.height,
                poster_object_key=poster_object_key,
            )
            session.add(media)
            await session.flush()  # need media.id for the block's FK
            session.add(
                PostBlock(
                    post_id=post.id,
                    position=position,
                    block_type="media",
                    media_id=media.id,
                )
            )
        else:
            session.add(
                PostBlock(
                    post_id=post.id,
                    position=position,
                    block_type="text",
                    text=block.text,
                )
            )
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


@router.get("/mine", response_model=list[PostRead])
async def get_my_posts(
    session: CurrentAsyncSession,
    user: CurrentVerifiedUser,
    skip: int = 0,
    limit: int = 20,
):
    """The current user's own posts, newest first — backs the "Posted" history screen.

    Declared before `/{post_id}` so the literal path wins the route match.
    """
    posts = (
        (
            await session.execute(
                select(Post)
                .options(
                    selectinload(Post.channel),
                    selectinload(Post.author),
                    selectinload(Post.blocks).selectinload(PostBlock.media),
                )
                .filter(Post.author_id == user.id)
                .order_by(Post.created.desc())
                .offset(skip)
                .limit(limit)
            )
        )
        .scalars()
        .all()
    )
    return [_serialize_post(p, user) for p in posts]


@router.get("/reviewed", response_model=list[ReviewedPostRead])
async def get_my_reviewed_posts(
    session: CurrentAsyncSession,
    user: CurrentVerifiedUser,
    skip: int = 0,
    limit: int = 20,
):
    """Posts the current user has forwarded or dropped, ordered by review time (not
    post creation time) — backs the "Reviews" history screen.

    Declared before `/{post_id}` so the literal path wins the route match.
    """
    reviews = (
        (
            await session.execute(
                select(PostReview)
                .options(
                    selectinload(PostReview.post).selectinload(Post.channel),
                    selectinload(PostReview.post).selectinload(Post.author),
                    selectinload(PostReview.post)
                    .selectinload(Post.blocks)
                    .selectinload(PostBlock.media),
                )
                .filter(PostReview.user_id == user.id)
                .order_by(PostReview.created.desc())
                .offset(skip)
                .limit(limit)
            )
        )
        .scalars()
        .all()
    )
    return [
        ReviewedPostRead(
            post=_serialize_post(r.post, user),
            kind=r.kind,
            reviewed_at=r.created,
        )
        for r in reviews
    ]


@router.get("/{post_id}", response_model=PostRead)
async def get_post(
    post_id: int,
    session: CurrentAsyncSession,
    user: CurrentVerifiedUser,
    redis: CurrentRedis,
):
    post = await _get_post_with_relations(session, post_id)
    if not post or not await _can_view_post(session, redis, user, post):
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
