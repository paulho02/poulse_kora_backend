"""`DELETE /users/me` — erasing an account, with or without its posts.

The two branches differ in exactly one thing (do the posts survive?), so most of
what is asserted here is the same either way: the account is gone from Postgres,
its distribution state is gone from Redis, and its bucket objects are gone.
"""

import uuid

import pytest
from httpx import AsyncClient
from redis.asyncio import Redis
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core import probes
from app.core.config import settings
from app.core.storage import StorageError, storage
from app.feed import keys, service
from app.models.channel import Channel
from app.models.feedback import Feedback
from app.models.item import Item
from app.models.oauth_account import GOOGLE_OAUTH_NAME, OAuthAccount
from app.models.post import Post
from app.models.post_block import PostBlock
from app.models.post_media import PostMedia
from app.models.post_review import PostReview
from app.models.probe_response import ProbeResponse
from app.models.user import User
from tests.utils import (
    generate_random_string,
    get_jwt_header,
    make_test_png,
    review,
    subscribe,
)

DELETE_ME = settings.API_PATH + "/users/me"


async def _count(db: AsyncSession, model, **filters) -> int:
    """Row count straight from the database.

    A `select(func.count())` rather than `db.get`/`db.refresh`: the route commits
    through its *own* session, so the identity map this session holds is a
    snapshot from before the deletion and would happily answer with a row that no
    longer exists.
    """
    stmt = select(func.count()).select_from(model)
    for column, value in filters.items():
        stmt = stmt.where(getattr(model, column) == value)
    return await db.scalar(stmt)


async def _link_google(db: AsyncSession, user: User) -> OAuthAccount:
    account = OAuthAccount(
        user_id=user.id,
        oauth_name=GOOGLE_OAUTH_NAME,
        access_token="unused",
        account_id=generate_random_string(21),
        account_email=user.email,
    )
    db.add(account)
    await db.commit()
    return account


class TestDeleteAccountGate:
    async def test_password_account_must_send_its_password(
        self, client: AsyncClient, db: AsyncSession, create_user
    ):
        user: User = await create_user()
        resp = await client.request(
            "DELETE", DELETE_ME, json={"delete_posts": False},
            headers=get_jwt_header(user),
        )
        assert resp.status_code == 400, resp.text
        assert resp.json()["detail"]["error"] == "delete_account_password_required"
        assert await _count(db, User, id=user.id) == 1

    async def test_wrong_password_leaves_the_account_alone(
        self, client: AsyncClient, db: AsyncSession, create_user
    ):
        user: User = await create_user()
        resp = await client.request(
            "DELETE", DELETE_ME,
            json={"delete_posts": True, "current_password": "not-it"},
            headers=get_jwt_header(user),
        )
        assert resp.status_code == 400, resp.text
        assert resp.json()["detail"]["error"] == "delete_account_wrong_password"
        assert await _count(db, User, id=user.id) == 1

    async def test_delete_posts_is_required(
        self, client: AsyncClient, create_user, default_password
    ):
        """No default for the one field that decides what other people keep
        seeing - a client that forgets it is a bug, not a preference."""
        user: User = await create_user()
        resp = await client.request(
            "DELETE", DELETE_ME, json={"current_password": default_password},
            headers=get_jwt_header(user),
        )
        assert resp.status_code == 422, resp.text

    async def test_requires_authentication(self, client: AsyncClient):
        resp = await client.request("DELETE", DELETE_ME, json={"delete_posts": False})
        assert resp.status_code == 401, resp.text

    async def test_google_account_needs_no_password(
        self, client: AsyncClient, db: AsyncSession, create_user
    ):
        """There is no password to prove: linking overwrote the hash with a random
        value nobody holds, so demanding one would refuse every Google account."""
        user: User = await create_user()
        await _link_google(db, user)
        resp = await client.request(
            "DELETE", DELETE_ME, json={"delete_posts": False},
            headers=get_jwt_header(user),
        )
        assert resp.status_code == 204, resp.text
        assert await _count(db, User, id=user.id) == 0
        assert await _count(db, OAuthAccount, user_id=user.id) == 0

    async def test_unverified_account_can_still_be_deleted(
        self, client: AsyncClient, db: AsyncSession, create_user, default_password
    ):
        user: User = await create_user(is_verified=False)
        resp = await client.request(
            "DELETE", DELETE_ME,
            json={"delete_posts": False, "current_password": default_password},
            headers=get_jwt_header(user),
        )
        assert resp.status_code == 204, resp.text
        assert await _count(db, User, id=user.id) == 0


class TestDeleteAccountKeepingPosts:
    async def test_posts_survive_without_their_author(
        self, client: AsyncClient, db: AsyncSession, create_user, create_channel,
        create_post, default_password,
    ):
        user: User = await create_user()
        channel: Channel = await create_channel()
        post: Post = await create_post(channel=channel, author=user)

        resp = await client.request(
            "DELETE", DELETE_ME,
            json={"delete_posts": False, "current_password": default_password},
            headers=get_jwt_header(user),
        )
        assert resp.status_code == 204, resp.text

        assert await _count(db, User, id=user.id) == 0
        assert await _count(db, Post, id=post.id) == 1
        assert await db.scalar(select(Post.author_id).where(Post.id == post.id)) is None

    async def test_a_reader_sees_the_kept_post_with_no_author(
        self, client: AsyncClient, redis: Redis, create_user, create_channel,
        create_post, default_password,
    ):
        """The erasure has to reach the *reader*, not just the table: the post is
        still deliverable, and must render like an anonymous one."""
        author: User = await create_user()
        reader: User = await create_user()
        channel: Channel = await create_channel()
        post: Post = await create_post(channel=channel, author=author)
        await service.place_post(redis, str(reader.id), post.id)

        resp = await client.request(
            "DELETE", DELETE_ME,
            json={"delete_posts": False, "current_password": default_password},
            headers=get_jwt_header(author),
        )
        assert resp.status_code == 204, resp.text

        feed = await client.get(
            settings.API_PATH + "/posts/feed", headers=get_jwt_header(reader)
        )
        assert feed.status_code == 200, feed.text
        [entry] = [e for e in feed.json() if e["post_id"] == post.id]
        assert entry["post"] is not None
        assert entry["post"]["author"] == {
            "id": None, "username": None, "profile_picture_url": None,
        }

    async def test_kept_posts_are_not_tombstoned_for_the_worker(
        self, client: AsyncClient, redis: Redis, create_user, create_channel,
        create_post, default_password,
    ):
        """Keeping the posts means they keep circulating - the only branch that
        stops in-flight fan-out is the one that erases them."""
        user: User = await create_user()
        channel: Channel = await create_channel()
        post: Post = await create_post(channel=channel, author=user)

        resp = await client.request(
            "DELETE", DELETE_ME,
            json={"delete_posts": False, "current_password": default_password},
            headers=get_jwt_header(user),
        )
        assert resp.status_code == 204, resp.text
        assert await service.is_post_deleted(redis, post.id) is False


class TestDeleteAccountErasingPosts:
    async def test_posts_blocks_media_and_reviews_all_go(
        self, client: AsyncClient, db: AsyncSession, create_user, create_channel,
        create_post, default_password,
    ):
        user: User = await create_user()
        channel: Channel = await create_channel()
        post: Post = await create_post(
            channel=channel,
            author=user,
            media=[{
                "media_type": "image", "content_type": "image/png",
                "size_bytes": 8, "data": make_test_png(),
            }],
        )
        reviewer: User = await create_user()
        await review(db, reviewer, post, "forward")

        resp = await client.request(
            "DELETE", DELETE_ME,
            json={"delete_posts": True, "current_password": default_password},
            headers=get_jwt_header(user),
        )
        assert resp.status_code == 204, resp.text

        assert await _count(db, Post, id=post.id) == 0
        assert await _count(db, PostBlock, post_id=post.id) == 0
        assert await _count(db, PostMedia, post_id=post.id) == 0
        # Somebody else's review row, but of a post that no longer exists.
        assert await _count(db, PostReview, post_id=post.id) == 0

    async def test_media_objects_leave_the_bucket(
        self, client: AsyncClient, db: AsyncSession, create_user, create_channel,
        create_post, default_password,
    ):
        user: User = await create_user()
        channel: Channel = await create_channel()
        post: Post = await create_post(
            channel=channel,
            author=user,
            media=[{
                "media_type": "image", "content_type": "image/png",
                "size_bytes": 8, "data": make_test_png(),
            }],
        )
        key = await db.scalar(
            select(PostMedia.object_key).where(PostMedia.post_id == post.id)
        )
        assert await storage.get_object(key)

        resp = await client.request(
            "DELETE", DELETE_ME,
            json={"delete_posts": True, "current_password": default_password},
            headers=get_jwt_header(user),
        )
        assert resp.status_code == 204, resp.text

        with pytest.raises(StorageError):
            await storage.get_object(key)

    async def test_profile_picture_leaves_the_bucket_either_way(
        self, client: AsyncClient, create_user, default_password
    ):
        user: User = await create_user()
        upload = await client.put(
            settings.API_PATH + "/users/me/profile-picture",
            files={"file": ("avatar.png", make_test_png(), "image/png")},
            headers=get_jwt_header(user),
        )
        assert upload.status_code == 200, upload.text
        url = upload.json()["profile_picture_url"]
        key = url.split("?")[0].split(f"{settings.STORAGE_BUCKET}/", 1)[1]
        assert await storage.get_object(key)

        resp = await client.request(
            "DELETE", DELETE_ME,
            json={"delete_posts": False, "current_password": default_password},
            headers=get_jwt_header(user),
        )
        assert resp.status_code == 204, resp.text

        with pytest.raises(StorageError):
            await storage.get_object(key)

    async def test_erased_posts_are_tombstoned_for_the_worker(
        self, client: AsyncClient, redis: Redis, create_user, create_channel,
        create_post, default_password,
    ):
        """Operations minted before the deletion are still in the stream, and the
        worker never reads Postgres - the tombstone is the only way it can learn."""
        user: User = await create_user()
        channel: Channel = await create_channel()
        post: Post = await create_post(channel=channel, author=user)
        await redis.sadd(keys.seen(post.id), str(uuid.uuid4()))

        resp = await client.request(
            "DELETE", DELETE_ME,
            json={"delete_posts": True, "current_password": default_password},
            headers=get_jwt_header(user),
        )
        assert resp.status_code == 204, resp.text

        assert await service.is_post_deleted(redis, post.id) is True
        assert await redis.exists(keys.seen(post.id)) == 0
        ttl = await redis.ttl(keys.deleted_post(post.id))
        assert 0 < ttl <= settings.FEED_RETRY_MAX_AGE_SECONDS


class TestAuthorlessPostsKeepCirculating:
    """The posts kept by a departing author are still ordinary posts.

    Worth pinning separately because a NULL `author_id` breaks SQL comparisons
    in the direction nobody notices: `NULL != <uuid>` is NULL, so a plain
    inequality silently *excludes* the row rather than including it.
    """

    async def test_a_rebuild_still_backfills_them(
        self, client: AsyncClient, db: AsyncSession, redis: Redis, create_user,
        create_channel, create_post, default_password,
    ):
        author: User = await create_user()
        reader: User = await create_user()
        channel: Channel = await create_channel()
        post: Post = await create_post(channel=channel, author=author)
        await subscribe(db, reader, channel)

        resp = await client.request(
            "DELETE", DELETE_ME,
            json={"delete_posts": False, "current_password": default_password},
            headers=get_jwt_header(author),
        )
        assert resp.status_code == 204, resp.text

        await service.rebuild_from_pg(redis, db)
        assert post.id in await service.render_queue_ids(redis, str(reader.id), 20)

    async def test_forwarding_one_still_fans_it_out(
        self, client: AsyncClient, db: AsyncSession, redis: Redis, create_user,
        create_channel, create_post, default_password,
    ):
        author: User = await create_user()
        reader: User = await create_user()
        channel: Channel = await create_channel()
        post: Post = await create_post(channel=channel, author=author)
        await service.place_post(redis, str(reader.id), post.id)

        deleted = await client.request(
            "DELETE", DELETE_ME,
            json={"delete_posts": False, "current_password": default_password},
            headers=get_jwt_header(author),
        )
        assert deleted.status_code == 204, deleted.text

        resp = await client.post(
            settings.API_PATH + f"/posts/{post.id}/review",
            headers=get_jwt_header(reader),
            json={"kind": "forward"},
        )
        assert resp.status_code == 200, resp.text
        # The op carries no author to exclude, rather than the string "None".
        entries = await redis.xrange(keys.STREAM)
        assert "author_id" not in entries[-1][1]


class TestDeleteAccountSideTables:
    async def test_redis_distribution_state_is_purged(
        self, client: AsyncClient, db: AsyncSession, redis: Redis, create_user,
        create_channel, create_post, default_password,
    ):
        user: User = await create_user()
        channel: Channel = await create_channel()
        other: Post = await create_post(channel=channel)
        await subscribe(db, user, channel)
        await service.sync_subscribe(redis, str(user.id), channel.id, ["en"])
        await service.place_post(redis, str(user.id), other.id)
        await service.earn_token(redis, str(user.id), 3)
        await service.mark_active(redis, str(user.id))
        # Two memberships from one subscription: the channel set and the one language
        # this reader accepts (see keys.SUBS_TOTAL).
        assert await redis.get(keys.SUBS_TOTAL) == "2"

        resp = await client.request(
            "DELETE", DELETE_ME,
            json={"delete_posts": False, "current_password": default_password},
            headers=get_jwt_header(user),
        )
        assert resp.status_code == 204, resp.text

        assert await redis.exists(keys.queue(str(user.id))) == 0
        assert await redis.exists(keys.tokens(str(user.id))) == 0
        assert await redis.sismember(keys.FREE_QUEUE, str(user.id)) == 0
        assert await redis.zscore(keys.ACTIVE_USERS, str(user.id)) is None
        assert await redis.sismember(keys.channel(channel.id), str(user.id)) == 0
        # Every language slice too, not only the channel set - a membership left
        # behind here would keep fan-out selecting an account that no longer exists.
        assert await redis.sismember(keys.audience(channel.id, "en"), str(user.id)) == 0
        # The running denominator of the price formula: a departure that went
        # unrecorded here would overstate subscriptions forever.
        assert await redis.get(keys.SUBS_TOTAL) == "0"

    async def test_answered_test_posts_go_with_the_account(
        self, client: AsyncClient, db: AsyncSession, redis: Redis, create_user,
        create_channel, default_password,
    ):
        """A probe is minted for one named reader, so "this person was tested and got
        it wrong" is a fact about them and goes when they do - regardless of the
        keep-my-posts choice, which is about what the account *published*."""
        user: User = await create_user()
        channel: Channel = await create_channel()
        await subscribe(db, user, channel)
        await db.refresh(user)
        probe = await probes.mint_probe(db, redis, user)
        assert probe is not None
        await client.post(
            settings.API_PATH + f"/posts/{probe.id}/review",
            headers=get_jwt_header(user),
            json={"kind": "drop"},
        )
        assert await _count(db, ProbeResponse, user_id=user.id) == 1

        resp = await client.request(
            "DELETE", DELETE_ME,
            json={"delete_posts": False, "current_password": default_password},
            headers=get_jwt_header(user),
        )
        assert resp.status_code == 204, resp.text

        assert await _count(db, ProbeResponse, user_id=user.id) == 0
        assert await _count(db, Post, id=probe.id) == 0
        assert await _count(db, PostBlock, post_id=probe.id) == 0
        # The probe bookkeeping in Redis too, or a deleted account's successor at the
        # same id would inherit its gap counter and pending flag.
        assert await redis.exists(probes._pending_key(str(user.id))) == 0
        assert await redis.exists(probes._since_key(str(user.id))) == 0
        assert await redis.exists(probes._recent_key(str(user.id))) == 0

    async def test_own_reviews_and_items_go(
        self, client: AsyncClient, db: AsyncSession, create_user, create_channel,
        create_post, create_item, default_password,
    ):
        user: User = await create_user()
        channel: Channel = await create_channel()
        someone_elses: Post = await create_post(channel=channel)
        await review(db, user, someone_elses, "drop")
        await create_item(user=user)

        resp = await client.request(
            "DELETE", DELETE_ME,
            json={"delete_posts": False, "current_password": default_password},
            headers=get_jwt_header(user),
        )
        assert resp.status_code == 204, resp.text

        assert await _count(db, PostReview, user_id=user.id) == 0
        assert await _count(db, Item, user_id=user.id) == 0
        # The post they reviewed keeps the score that review produced: it says how
        # the post fared, not who is still around to vouch for it.
        assert await db.scalar(
            select(Post.dropped_count).where(Post.id == someone_elses.id)
        ) == 1

    async def test_feedback_survives_without_the_reporter(
        self, client: AsyncClient, db: AsyncSession, create_user, default_password
    ):
        """A bug report outlives the account that filed it - only the ability to
        write back goes (see the Feedback model's docstring)."""
        user: User = await create_user()
        report = Feedback(
            kind="bug",
            message="something broke",
            user_id=user.id,
            allow_contact=True,
            user_agreed_data_saving_at=func.now(),
        )
        db.add(report)
        await db.commit()
        report_id = report.id

        resp = await client.request(
            "DELETE", DELETE_ME,
            json={"delete_posts": False, "current_password": default_password},
            headers=get_jwt_header(user),
        )
        assert resp.status_code == 204, resp.text

        assert await _count(db, Feedback, id=report_id) == 1
        assert await db.scalar(
            select(Feedback.user_id).where(Feedback.id == report_id)
        ) is None

    async def test_the_token_stops_working(
        self, client: AsyncClient, create_user, default_password
    ):
        user: User = await create_user()
        header = get_jwt_header(user)
        resp = await client.request(
            "DELETE", DELETE_ME,
            json={"delete_posts": False, "current_password": default_password},
            headers=header,
        )
        assert resp.status_code == 204, resp.text

        after = await client.get(settings.API_PATH + "/users/me", headers=header)
        assert after.status_code == 401, after.text
