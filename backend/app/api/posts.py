import time
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, File, Form, UploadFile
from pydantic import TypeAdapter, ValidationError
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import selectinload

from app.core import languages, probes, trust_service
from app.core.config import settings
from app.core.errors import api_error
from app.core.logger import get_logger
from app.core.media_validation import ProcessedMedia, process_upload
from app.core.review_rules import is_review_gate_unlocked
from app.core.storage import (
    MEDIA_CACHE_CONTROL,
    StorageError,
    post_media_key,
    storage,
)
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
    FeedEntry,
    FeedStatus,
    PostAuthor,
    PostBlockIn,
    PostBlockRead,
    PostCreate,
    PostCreateResult,
    PostEconomy,
    PostPrice,
    PostMediaRead,
    PostRead,
)
from app.schemas.post_review import PostReviewCreate, PostReviewResult, ReviewedPostRead

log = get_logger(__name__)

router = APIRouter(prefix="/posts")

_blocks_adapter = TypeAdapter(list[PostBlockIn])



async def post_create_form(
    channel_id: int = Form(...),
    blocks: str = Form(...),
    language: str = Form(...),
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
    return PostCreate(
        channel_id=channel_id,
        blocks=parsed_blocks,
        language=language,
        is_anonymous=is_anonymous,
    )


def _serialize_post(post: Post, viewer: User) -> PostRead:
    # `post.author is not None` is the account-deleted case (see
    # app/core/account_deletion.py): the row survived, the person did not.
    # Folding it into the existing anonymity branch is the whole handling - to a
    # reader it is the same post with no name on it, and nothing downstream had
    # to learn a third shape.
    reveal_author = post.author is not None and (
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
        language=post.language,
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
        is_probe=post.is_probe,
        # Withheld with the author: at launch the supporter set is small enough that
        # "anonymous, but a supporter" narrows the author to a handful of accounts.
        subscription_kind=post.subscription_kind if reveal_author else None,
        # The author's alone - not even a superuser's, who sees the author but has
        # no business with the crowd's verdict. See PostRead.gifted_count.
        gifted_count=post.gifted_count if post.author_id == viewer.id else None,
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
                cache_control=MEDIA_CACHE_CONTROL,
            )
            written.append(key)

            poster_key = None
            if item.poster and item.poster_content_type:
                poster_key = post_media_key(item.poster_content_type)
                await storage.put_object(
                    poster_key,
                    item.poster,
                    content_type=item.poster_content_type,
                    cache_control=MEDIA_CACHE_CONTROL,
                )
                written.append(poster_key)
            keys[index] = (key, poster_key)
    except StorageError:
        log.exception("post.media_upload_failed", discarded_objects=len(written))
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


@router.get("/feed", response_model=list[FeedEntry])
async def get_posts_feed(
    session: CurrentAsyncSession,
    user: CurrentVerifiedUser,
    redis: CurrentRedis,
    channel_id: int | None = None,
    skip: int = 0,
    limit: int | None = None,
):
    """Return the user's review queue, newest delivery first, rendered from Postgres
    by ID.

    The order is the queue's own and nothing re-sorts it: `place_post` LPUSHes, so
    index 0 is the most recently *placed* post - which is not quite the most
    recently authored one, since a forwarded post is placed long after it was
    written. Clients that append arrivals to the bottom of what is already on
    screen (the Flutter app does, so nothing jumps under a reader mid-sentence)
    should therefore treat this order as the starting point rather than as a
    sort to re-apply on every fetch.

    The queue is maintained in Redis by the distribution worker; here we just read
    the post_ids and hydrate them. `place_post` dedupes on insert, so a post appears
    at most once in the queue even when fan-out and backfill both deliver it.

    Also marks the user as active (see service.mark_active) — this is the sole
    signal for the active-user estimate the admission-price formula reads.

    `skip`/`limit` page the *filtered* result, which is why the whole queue is read
    from Redis rather than just the requested slice: a channel filter applied after
    the slice would silently return fewer than `limit` posts and leave the rest of
    that channel unreachable at any offset. Reading it whole costs nothing worth
    saving — the queue is capped at FEED_QUEUE_MAX_SLOTS by construction.

    That cap is also `limit`'s default, so a client that omits it always holds the
    whole queue and can tell a genuine arrival from something it simply never asked
    for. A page size hardcoded client-side would quietly stop topping up the day the
    cap is raised past it.

    An id in the queue with no row behind it is answered as an empty `FeedEntry`
    rather than dropped, so the reader can clear the slot (see
    `dismiss_missing_post`). Silently skipping it - what this used to do - left a
    queue slot occupied by something the reader could neither see nor review, and
    the only symptom was a feed that stopped topping up. Only when no channel
    filter is applied, though: a vanished post has no channel left to belong to,
    so claiming it belongs to the one being filtered for would be an invention.
    """
    limit = settings.FEED_QUEUE_MAX_SLOTS if limit is None else limit
    await service.mark_active(redis, str(user.id))
    queue_ids = await service.render_queue_ids(
        redis, str(user.id), settings.FEED_QUEUE_MAX_SLOTS
    )
    if not queue_ids:
        # DEBUG, not INFO: an empty queue is the economy working as intended (see
        # the feed notes in CLAUDE.md), and this route is called far too often for
        # an INFO line. It is here at all because "my feed is empty" is a report
        # that arrives regularly and is otherwise unanswerable after the fact.
        log.debug("feed.served", queued=0, returned=0, channel_id=channel_id)
        return []

    filters = [Post.id.in_(queue_ids)]
    if channel_id is not None:
        filters.append(Post.channel_id == channel_id)
    posts = (
        (
            await session.execute(
                select(Post)
                .options(
                    selectinload(Post.channel),
                    selectinload(Post.author),
                    selectinload(Post.blocks).selectinload(PostBlock.media),
                )
                .filter(*filters)
            )
        )
        .scalars()
        .all()
    )
    by_id = {p.id: p for p in posts}
    # `by_id` is the *filtered* result, so a missing id means one of two things:
    # the post is gone, or it belongs to another channel. Only the unfiltered
    # view can tell those apart, which is why only it reports holes.
    ordered = [
        (pid, by_id.get(pid))
        for pid in queue_ids
        if pid in by_id or channel_id is None
    ]
    page = ordered[skip : skip + limit]
    missing = sum(1 for _, post in page if post is None)
    log.debug(
        "feed.served",
        queued=len(queue_ids),
        returned=len(page),
        missing=missing,
        channel_id=channel_id,
    )
    return [
        FeedEntry(
            post_id=pid,
            post=_serialize_post(post, user) if post is not None else None,
        )
        for pid, post in page
    ]


@router.get("/feed/status", response_model=FeedStatus)
async def get_feed_status(user: CurrentVerifiedUser, redis: CurrentRedis):
    """What is in the review queue right now, without rendering any of it.

    This is what lets the feed keep itself current instead of waiting to be pulled:
    a client polls it while the feed is on screen and only fetches `GET /posts/feed`
    when an id it has not already pulled shows up. One LRANGE, no Postgres, no
    presigning — cheap enough to poll, which is the entire point, since the same
    check against `/posts/feed` would re-serialize and re-sign the whole queue every
    time to answer "nothing new".

    Marks the user active for the same reason the feed read does: someone watching
    the feed is a reader whether or not they have pulled anything this minute.
    """
    await service.mark_active(redis, str(user.id))
    post_ids = await service.render_queue_ids(
        redis, str(user.id), settings.FEED_QUEUE_MAX_SLOTS
    )
    return FeedStatus(post_ids=post_ids, capacity=settings.FEED_QUEUE_MAX_SLOTS)


@router.delete("/feed/{post_id}", status_code=204)
async def dismiss_missing_post(
    post_id: int,
    session: CurrentAsyncSession,
    user: CurrentVerifiedUser,
    redis: CurrentRedis,
):
    """Clear a queue slot whose post no longer exists - the ghost card's one
    button.

    Not a review, and deliberately not routed through `POST /{post_id}/review`:
    a `PostReview` row has a foreign key to a post that is gone, there is no
    verdict to record about something nobody read, and nothing is earned for it.
    The reader is only taking back a slot that an author's account deletion left
    occupied.

    Refuses while the post is still there (`post_available`), so this cannot
    become a way to skip a post without judging it. That check is also what makes
    the route safe to leave outside the interaction budget: a whole queue of
    ghosts is up to FEED_QUEUE_MAX_SLOTS of them, which at
    INTERACTION_RATE_LIMIT would throttle a reader for the better part of a
    minute over a mess somebody else made.
    """
    if await session.get(Post, post_id) is not None:
        raise api_error(409, "post_available")
    removed = await service.claim_from_queue(redis, str(user.id), post_id)
    if removed == 0:
        raise api_error(409, "not_in_queue")
    # INFO, and rare by construction: it takes an account deletion to produce
    # one. A run of them is how "posts are vanishing from my feed" gets a cause.
    log.info("feed.ghost_dismissed", post_id=post_id)


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

    The price charged is this *route's* price for the current window (see
    service.route_prices), not a fresh live computation — the same number
    `GET /posts/price` quoted for this channel and language, rather than one that could
    have drifted in the seconds or minutes between composing and posting.

    `language` is required and self-declared; the client detects it and lets the author
    override. Only two things are checked here: that it is a configured content
    language (or the reserved "no language" value), and that "no language" is not
    claimed by a post that contains text.

    Shares the per-user interaction budget with reviewing (see app/deps/rate_limit.py),
    so a burst of posts and forwards together still can't flood the queue."""
    channel = await session.get(Channel, post_in.channel_id)
    if not channel:
        raise api_error(404, "channel_not_found")

    if not languages.is_post_language(post_in.language):
        raise api_error(400, "post_language_invalid")

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

    if post_in.language == languages.UNSPECIFIED and any(
        b.type == "text" for b in blocks
    ):
        # "No language" routes through the whole channel rather than one language's
        # readers, so it is the widest audience a post can reach - which makes it the
        # thing to claim falsely. Text is the one part of the claim that is checkable
        # without reading the post, so it is the one part enforced. Text baked into an
        # image still gets through; the forward/drop economy is what answers that, and
        # the ratio of dropped UNSPECIFIED posts is the metric that would show it
        # happening.
        #
        # Note the converse is deliberately *not* enforced: a post with no text may
        # still declare a real language, because a video can be spoken German.
        raise api_error(400, "post_language_requires_no_text")

    # Only a video block's `orientation` is honoured (see PostBlockIn); the map is
    # keyed by file index because that, not block position, is what identifies a
    # file in the parallel `files` list.
    orientation_by_index = {
        block.file_index: block.orientation for block in media_blocks
    }

    processed_by_index: dict[int, ProcessedMedia] = {}
    total_media_bytes = 0
    # Validation/transcode is by far the most expensive thing this route does
    # (ffmpeg, in-process, holding the clip in memory), so it is timed separately
    # from the request as a whole - "posting is slow" is nearly always this.
    media_started = time.perf_counter()
    for index, file in enumerate(files):
        item = await process_upload(file, orientation_by_index.get(index))
        total_media_bytes += item.size_bytes
        if total_media_bytes > settings.POST_MEDIA_MAX_TOTAL_BYTES:
            raise api_error(400, "post_media_total_too_large")
        processed_by_index[index] = item
    media_ms = round((time.perf_counter() - media_started) * 1000, 1)

    route = (post_in.channel_id, post_in.language)
    price = (await service.route_prices(redis, [route]))[route]
    if user.is_superuser:
        token_balance = await service.token_balance(redis, str(user.id))
    else:
        token_balance = await service.spend_tokens(redis, str(user.id), price)
        if token_balance is None:
            balance = await service.token_balance(redis, str(user.id))
            # Not an error - the economy working as designed - but the rate of it
            # is the signal that says whether the price is set anywhere near right.
            log.info(
                "post.rejected_insufficient_tokens",
                channel_id=post_in.channel_id,
                price=price,
                balance=balance,
            )
            raise api_error(402, "insufficient_tokens", balance=balance, price=price)

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
        language=post_in.language,
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
        redis, post.id, post.channel_id, post.language, author_id=str(post.author_id)
    )
    log.info(
        "post.created",
        post_id=post.id,
        channel_id=post.channel_id,
        language=post.language,
        price=price,
        token_balance=token_balance,
        blocks=len(blocks),
        media_files=len(files),
        media_bytes=total_media_bytes,
        media_ms=media_ms,
        is_anonymous=post.is_anonymous,
        supporter=is_supporter,
    )

    post = await _get_post_with_relations(session, post.id)
    return PostCreateResult(
        post=_serialize_post(post, user),
        price=price,
        token_balance=token_balance,
    )


@router.get("/economy", response_model=PostEconomy)
async def get_post_economy(user: CurrentVerifiedUser, redis: CurrentRedis):
    """Current spendable token balance and what publishing costs across the
    deployment: the shared base price, the observed range around it, and the instant
    that base price stops being guaranteed (see service.get_price_snapshot).

    The range is *observed*, not enumerated - it covers every route someone has priced
    (see keys.PRICE_RANGE), and converges on the exact spread at the first `GET
    /channels` of the window, which the client issues constantly. Until then it is the
    previous window's spread rescaled onto the current base price; both ends fall back
    to the base price itself only where nothing has ever been observed, which is also
    exactly what a deployment with a single route would report.

    Declared before `/{post_id}` so the literal path wins the route match.
    """
    snapshot = await service.get_price_snapshot(redis)
    balance = await service.token_balance(redis, str(user.id))
    observed = await service.read_price_range(redis)
    low, high = observed if observed else (snapshot["price"], snapshot["price"])
    return PostEconomy(
        token_balance=balance,
        post_price=snapshot["price"],
        post_price_min=low,
        post_price_max=high,
        post_price_expires_at=datetime.fromtimestamp(
            snapshot["expires_at"], tz=timezone.utc
        ),
    )


@router.get("/price", response_model=PostPrice)
async def get_post_price(
    channel_id: int,
    language: str,
    session: CurrentAsyncSession,
    user: CurrentVerifiedUser,
    redis: CurrentRedis,
):
    """The exact price to publish in one (channel, language) route.

    The counterpart to the range on `GET /channels`: a range is all that can be quoted
    before an author has chosen a language, and this is the number once they have. It
    is a quote rather than an estimate - `POST /posts` charges exactly this until
    `expires_at`, because both read the same cached route price for the same window.

    Validates the channel and the language rather than pricing whatever it is given:
    an unknown route would otherwise be priced at the neutral factor and quote a
    plausible number for a post that could never be created.

    Declared before `/{post_id}` so the literal path wins the route match.
    """
    if not await session.get(Channel, channel_id):
        raise api_error(404, "channel_not_found")
    if not languages.is_post_language(language):
        raise api_error(400, "post_language_invalid")

    route = (channel_id, language)
    price = (await service.route_prices(redis, [route]))[route]
    snapshot = await service.get_price_snapshot(redis)
    return PostPrice(
        price=price,
        expires_at=datetime.fromtimestamp(snapshot["expires_at"], tz=timezone.utc),
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
                .limit(min(limit, settings.LIST_MAX_PAGE_SIZE))
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
                .limit(min(limit, settings.LIST_MAX_PAGE_SIZE))
            )
        )
        .scalars()
        .all()
    )
    return [
        ReviewedPostRead(
            post=_serialize_post(r.post, user),
            kind=r.kind,
            gifted=r.gifted,
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

    This is also the only route that discloses how the post has fared
    (`post_forwarded_count` / `post_reviewed_count`) - after the verdict, so the
    crowd cannot cast it. Keep it out of PostRead.

    A **test post** (see app/core/probes.py) takes the short branch below and touches
    almost none of this. It earns its token, because the attention it cost was real and
    a reader who happens to be measured more often should not end the month poorer for
    it - but it writes no `PostReview`, moves no counter on the reader or the post, and
    mints no fan-out however it was answered. That is what keeps `reviewed_count ==
    forwarded_count + dropped_count == COUNT(post_reviews)` true, keeps probes out of
    `GET /posts/reviewed`, and keeps the instrument out of the very pace and
    forward-rate signals it exists to calibrate.

    On the ordinary path, a **forward** is now worth what the forwarder's judgement is
    worth: `trust_service.forward_fanout` resolves their Reviewer Trust band into a
    recipient count that travels with the operation. Only a forward - an original post
    reaches exactly what its author paid the admission price for.

    A forward may also **gift** (`gift_token`): the token this review earns goes to the
    post's author instead of the reviewer. A transfer, not a mint - the economy's supply
    is unchanged, so gifting can't be farmed, and the reviewer pays with the very token
    the review earned, so it is always affordable. What it buys is voice: the author's
    next post is closer to its admission price. Refused as `gift_not_allowed` for a
    test post (its author is the system), the reviewer's own post (a no-op that would
    pad the gift record) and an author who has deleted their account (nobody to give
    to). Checked before the queue is touched, so a refusal leaves the post reviewable.
    """
    post = await session.get(Post, post_id)
    if not post:
        raise api_error(404, "post_not_found")

    if review_in.gift_token and (
        post.is_probe or post.author_id is None or post.author_id == user.id
    ):
        log.info("post.review_rejected", post_id=post_id, reason="gift_not_allowed")
        raise api_error(409, "gift_not_allowed")

    removed = await service.claim_from_queue(redis, str(user.id), post_id)
    if removed == 0:
        # Usually a double-tap or a stale client, but a run of them means the
        # queue and the screen have drifted apart, which is a real bug class.
        log.info("post.review_rejected", post_id=post_id, reason="not_in_queue")
        raise api_error(409, "not_in_queue")

    if post.is_probe:
        correct = await probes.answer(session, redis, user, post, review_in.kind)
        token_balance = await service.earn_token(redis, str(user.id))
        log.info(
            "post.reviewed",
            post_id=post_id,
            channel_id=post.channel_id,
            kind=review_in.kind,
            token_balance=token_balance,
            is_probe=True,
        )
        return PostReviewResult(
            post_id=post_id,
            kind=review_in.kind,
            # The reader's own counters, unchanged and read back as they stand. A probe
            # is not a review, and the profile tile must not claim otherwise.
            reviewed_count=user.reviewed_count,
            review_gate=settings.REVIEW_GATE,
            unlocked=is_review_gate_unlocked(user),
            token_balance=token_balance,
            # Zero rather than the row's real 0/1: a probe is minted for one reader and
            # nobody else will ever see it, so "1 of 1" would be a true number that
            # means nothing. The client shows the probe confirmation instead.
            post_forwarded_count=0,
            post_reviewed_count=0,
            is_probe=True,
            probe_correct=correct,
        )

    session.add(
        PostReview(
            user_id=user.id,
            post_id=post_id,
            kind=review_in.kind,
            gifted=review_in.gift_token,
        )
    )
    user.reviewed_count += 1
    # The post's counters are incremented SQL-side, unlike the user's: a post is
    # fanned out to FEED_FANOUT readers at once, so its row is the one here that
    # concurrent requests actually contend for, and a read-modify-write in Python
    # silently loses increments. Cheap insurance now that the numbers are shown
    # back to the reviewer rather than only summed into stats.
    if review_in.kind == "forward":
        post.forwarded_count = Post.forwarded_count + 1
        user.forwarded_count += 1
        if review_in.gift_token:
            post.gifted_count = Post.gifted_count + 1
    else:
        post.dropped_count = Post.dropped_count + 1
        user.dropped_count += 1
    try:
        await session.commit()
    except IntegrityError:
        # Re-delivery: a fan-out (e.g. a due ops:retry) put back a post this user had
        # already reviewed. It has been removed from the queue above; nothing to record.
        await session.rollback()
        # WARNING rather than INFO: the unique constraint is the *backstop* for the
        # seen-set exclusion, so hitting it means the set did its job badly (lost,
        # expired early, or never seeded - see FEED_EXCLUDE_SEEN in CLAUDE.md).
        log.warning("post.review_rejected", post_id=post_id, reason="already_reviewed")
        raise api_error(409, "already_reviewed") from None

    # The SQL-expression increments above leave those two attributes expired, and
    # an async session cannot load them lazily on attribute access - re-read them
    # explicitly (one indexed row, two ints) so the result can report them.
    await session.refresh(post, ["forwarded_count", "dropped_count"])

    if review_in.gift_token:
        # After the commit, like the reviewer's own earn: the unique constraint above
        # is what makes a token per review true, and a re-delivery must not pay twice.
        await service.earn_token(redis, str(post.author_id))
        token_balance = await service.token_balance(redis, str(user.id))
    else:
        token_balance = await service.earn_token(redis, str(user.id))

    if review_in.kind == "forward":
        # How far this particular forward travels, resolved once here rather than at
        # delivery: the worker has no Postgres session and so no way to score anyone,
        # and a reader's verdict should be worth what their judgement was worth when
        # they made it rather than what it has drifted to by the time the post finds an
        # audience. None when trust is disabled, which writes no field at all and leaves
        # the operation byte-identical to a pre-trust one.
        fanout = await trust_service.forward_fanout(session, redis, str(user.id))
        # The author travels with the post, not with whoever forwarded it — a forward
        # must still never land back on the person who wrote it. None once they have
        # deleted their account, which is the same as the pre-exclusions ops the
        # worker already tolerates: there is nobody left to withhold it from.
        await service.enqueue_operation(
            redis,
            post_id,
            post.channel_id,
            # Re-derived from the post, not carried from the op that delivered it: a
            # forward is a new operation for the same post, and the post's language is
            # what decides its audience however many hands it has passed through.
            post.language,
            author_id=str(post.author_id) if post.author_id else None,
            fanout=fanout,
        )

    # Roll for whether this reader's next post is a test. After the review has committed
    # and after the forward has been enqueued, so nothing about the measurement can cost
    # someone a review they already made - and wrapped, because a probe is worth a little
    # confidence in a score and is never worth failing a request over.
    try:
        if await probes.should_mint(redis, str(user.id)):
            await probes.mint_probe(session, redis, user)
    except Exception:
        log.exception("probe.mint_failed")

    log.info(
        "post.reviewed",
        post_id=post_id,
        channel_id=post.channel_id,
        kind=review_in.kind,
        token_balance=token_balance,
        gifted=review_in.gift_token,
    )

    return PostReviewResult(
        post_id=post_id,
        kind=review_in.kind,
        reviewed_count=user.reviewed_count,
        review_gate=settings.REVIEW_GATE,
        unlocked=is_review_gate_unlocked(user),
        token_balance=token_balance,
        post_forwarded_count=post.forwarded_count,
        post_reviewed_count=post.forwarded_count + post.dropped_count,
        gifted=review_in.gift_token,
    )
