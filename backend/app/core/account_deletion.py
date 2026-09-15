"""Erasing an account, and optionally everything it published.

One entry point, `delete_account`, behind `DELETE /users/me` (app/api/users.py).
It is irreversible on purpose: there is no soft-delete flag, no tombstone user
row and no grace period, because every one of those keeps the personal data this
is asked to destroy.

The user picks between two outcomes, and the difference is only ever about
*posts*:

- **Keep them** (`delete_posts=False`). `posts.author_id` is nulled, the account
  and its username go, and every post carries on through the feed with no author
  attached - indistinguishable, to a reader, from one published anonymously.
- **Erase them too** (`delete_posts=True`). The rows and their bucket objects go
  with the account. Copies already sitting in other readers' review queues are
  *not* hunted down (see `app/feed/service.mark_posts_deleted`); the feed answers
  those with a ghost entry the reader can dismiss.

Three things are load-bearing.

**Explicit bulk statements, not `session.delete(user)`.** The ORM cascade on
`User.posts` is `all, delete` - it would erase the posts on its way to the user
row, which is exactly the choice this function exists to let someone make. Doing
every table by hand also fixes the order (blocks before the media rows they
reference, children before parents) rather than leaving it to whichever
referential action Postgres happens to fire first, and keeps the whole erasure
readable as one list.

**Postgres commits before anything irreversible happens outside it.** Bucket
objects are deleted last, so a failed transaction leaves a live account with its
media intact rather than a live account whose pictures 404. The residual failure
mode is the cheap one: objects nobody references any more.

**Redis is state, not record.** It is purged after the commit, and a purge that
half-ran costs a stale queue entry or a slightly wrong price denominator - never
a resurrected account.

One thing this deliberately cannot reach: a test post still sitting *unanswered* in
the account's queue. Nothing in Postgres records who a probe was minted for until it
is answered - the link lives only in the reader's Redis queue, which is about to be
purged - so such a post is left behind as a row with no personal data attached to it
and nothing referencing it. That is deliberate rather than a gap: storing the link at
mint time would mean keeping a record of who was tested for every probe nobody ever
answered, which is more data about the user, not less. Nothing collects those rows
afterwards either, because nothing can tell them apart from a probe still sitting in a
*live* reader's queue, and deleting one of those would hand its reader a ghost card.

One thing is deliberately *not* undone: the forward/drop counters on posts this
user reviewed. `PostReview` rows are personal data and go, but the aggregate they
fed says how a post fared at the time, not who is still around to vouch for it -
walking those back would rewrite other people's scores as a side effect of
someone leaving.
"""

from dataclasses import dataclass

from redis.asyncio import Redis
from sqlalchemy import delete, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.core import probes
from app.core.logger import get_logger
from app.core.storage import storage
from app.feed import service
from app.models.channel_subscription import ChannelSubscription
from app.models.item import Item
from app.models.oauth_account import OAuthAccount
from app.models.post import Post
from app.models.post_block import PostBlock
from app.models.post_media import PostMedia
from app.models.post_review import PostReview
from app.models.probe_response import ProbeResponse
from app.models.supporter_subscription import SupporterSubscription
from app.models.user import User
from app.models.user_subscription import UserSubscription

log = get_logger(__name__)


@dataclass(frozen=True)
class DeletionSummary:
    """What the erasure actually did - for the log line, and for the tests."""

    posts_deleted: int
    posts_anonymized: int
    objects_deleted: int


async def _post_object_keys(session: AsyncSession, post_ids: list[int]) -> list[str]:
    """Every bucket object belonging to these posts: the media and its posters."""
    if not post_ids:
        return []
    rows = (
        await session.execute(
            select(PostMedia.object_key, PostMedia.poster_object_key).where(
                PostMedia.post_id.in_(post_ids)
            )
        )
    ).all()
    return [key for row in rows for key in row if key]


async def delete_account(
    session: AsyncSession, redis: Redis, user: User, *, delete_posts: bool
) -> DeletionSummary:
    """Erase `user`, and their posts too when `delete_posts` is set.

    The caller is responsible for having proved the request came from the account
    holder (see `DELETE /users/me`); by the time this runs the decision is made.
    """
    user_id = user.id
    profile_picture_key = user.profile_picture_key

    # Read before writing: both lists describe rows this function is about to
    # destroy, and both are needed *after* the commit - the channel ids to
    # unpick the Redis subscription sets, the post ids to tombstone the
    # in-flight fan-out operations.
    channel_ids = list(
        (
            await session.execute(
                select(ChannelSubscription.channel_id).where(
                    ChannelSubscription.user_id == user_id
                )
            )
        )
        .scalars()
        .all()
    )
    post_ids = list(
        (
            await session.execute(select(Post.id).where(Post.author_id == user_id))
        )
        .scalars()
        .all()
    )
    # Test posts this account answered (see app/core/probes.py). A probe is minted for
    # exactly one reader, so the post is as personal as the response to it - "this
    # person was tested and got it wrong" is a fact about them, and it goes with them.
    # Erased regardless of the keep-my-posts choice: that choice is about what this
    # account *published*, and nobody published these.
    probe_post_ids = list(
        (
            await session.execute(
                select(ProbeResponse.post_id).where(ProbeResponse.user_id == user_id)
            )
        )
        .scalars()
        .all()
    )

    object_keys: list[str] = []
    if profile_picture_key:
        object_keys.append(profile_picture_key)

    posts_deleted = 0
    posts_anonymized = 0
    if delete_posts and post_ids:
        object_keys.extend(await _post_object_keys(session, post_ids))
        # Blocks first: a media block references its `post_media` row, so the
        # reverse order trips that foreign key. Reviews next - they belong to
        # other people, but a review of a post that no longer exists cannot be
        # kept either.
        await session.execute(
            delete(PostBlock).where(PostBlock.post_id.in_(post_ids))
        )
        await session.execute(
            delete(PostMedia).where(PostMedia.post_id.in_(post_ids))
        )
        await session.execute(
            delete(PostReview).where(PostReview.post_id.in_(post_ids))
        )
        result = await session.execute(delete(Post).where(Post.id.in_(post_ids)))
        posts_deleted = result.rowcount or 0
    elif not delete_posts and post_ids:
        result = await session.execute(
            update(Post).where(Post.author_id == user_id).values(author_id=None)
        )
        posts_anonymized = result.rowcount or 0

    # Everything else the account owns. `feedback.user_id` is absent on purpose:
    # it is `ON DELETE SET NULL` in the schema, so the report survives without
    # the reporter - see the Feedback docstring for why that is the right shape
    # there and nowhere else.
    await session.execute(delete(PostReview).where(PostReview.user_id == user_id))
    await session.execute(
        delete(ProbeResponse).where(ProbeResponse.user_id == user_id)
    )
    if probe_post_ids:
        # Response rows first (they reference the post), then the block, then the post -
        # the same child-before-parent ordering the published-post branch above uses. A
        # probe never carries media, so there is no PostMedia pass and no bucket object.
        await session.execute(
            delete(PostBlock).where(PostBlock.post_id.in_(probe_post_ids))
        )
        await session.execute(delete(Post).where(Post.id.in_(probe_post_ids)))
    await session.execute(
        delete(ChannelSubscription).where(ChannelSubscription.user_id == user_id)
    )
    await session.execute(
        delete(UserSubscription).where(UserSubscription.user_id == user_id)
    )
    await session.execute(
        delete(SupporterSubscription).where(SupporterSubscription.user_id == user_id)
    )
    await session.execute(delete(Item).where(Item.user_id == user_id))
    await session.execute(delete(OAuthAccount).where(OAuthAccount.user_id == user_id))
    await session.execute(delete(User).where(User.id == user_id))
    await session.commit()

    # Past the point of no return. Everything below is cleanup of state that
    # merely mirrors what Postgres just stopped saying.
    await service.purge_user(redis, str(user_id), channel_ids)
    await probes.purge_user(redis, str(user_id))
    if delete_posts:
        await service.mark_posts_deleted(redis, post_ids)

    for key in object_keys:
        # Best-effort by contract (see storage.delete_object): a bucket that
        # refuses leaves an unreferenced object behind and logs, rather than
        # failing an erasure that has already happened in the database.
        await storage.delete_object(key)

    summary = DeletionSummary(
        posts_deleted=posts_deleted,
        posts_anonymized=posts_anonymized,
        objects_deleted=len(object_keys),
    )
    # The one line that says an account is gone, and the only place the fact is
    # recorded at all - there is no row left to ask. No email address on it, as
    # everywhere else; the id is what a later "did you really delete my data?"
    # is answered with.
    log.info(
        "account.deleted",
        user_id=str(user_id),
        delete_posts=delete_posts,
        posts_deleted=summary.posts_deleted,
        posts_anonymized=summary.posts_anonymized,
        objects_deleted=summary.objects_deleted,
        subscriptions=len(channel_ids),
    )
    return summary
