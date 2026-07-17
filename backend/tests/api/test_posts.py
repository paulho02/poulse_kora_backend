from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.models.channel import Channel
from app.models.post import Post
from app.models.user import User
from tests.utils import get_jwt_header, review, subscribe


class TestPostsFeed:
    async def test_feed_empty_with_no_subscriptions(
        self, client: AsyncClient, create_user
    ):
        user: User = await create_user()
        resp = await client.get(
            settings.API_PATH + "/posts/feed", headers=get_jwt_header(user)
        )
        assert resp.status_code == 200, resp.text
        assert resp.json() == []

    async def test_feed_excludes_unsubscribed_reviewed_and_own_posts(
        self,
        client: AsyncClient,
        db: AsyncSession,
        create_user,
        create_channel,
        create_post,
    ):
        user: User = await create_user()
        subscribed_channel: Channel = await create_channel()
        other_channel: Channel = await create_channel()
        await subscribe(db, user, subscribed_channel)

        visible: Post = await create_post(channel=subscribed_channel)
        already_reviewed: Post = await create_post(channel=subscribed_channel)
        await review(db, user, already_reviewed, "forward")
        await create_post(channel=other_channel)  # not subscribed
        own_post: Post = await create_post(channel=subscribed_channel, author=user)

        resp = await client.get(
            settings.API_PATH + "/posts/feed", headers=get_jwt_header(user)
        )
        assert resp.status_code == 200, resp.text
        ids = [p["id"] for p in resp.json()]
        assert visible.id in ids
        assert already_reviewed.id not in ids
        assert own_post.id not in ids

    async def test_feed_anonymous_post_hides_author_for_other_users(
        self,
        client: AsyncClient,
        db: AsyncSession,
        create_user,
        create_channel,
        create_post,
    ):
        viewer: User = await create_user()
        channel: Channel = await create_channel()
        await subscribe(db, viewer, channel)
        author: User = await create_user()
        post: Post = await create_post(channel=channel, author=author, is_anonymous=True)

        resp = await client.get(
            settings.API_PATH + "/posts/feed", headers=get_jwt_header(viewer)
        )
        assert resp.status_code == 200, resp.text
        [data] = [p for p in resp.json() if p["id"] == post.id]
        assert data["author"]["id"] is None
        assert data["author"]["username"] is None


class TestCreatePost:
    async def test_create_fails_not_subscribed(
        self, client: AsyncClient, create_user, create_channel
    ):
        user: User = await create_user()
        channel: Channel = await create_channel()
        resp = await client.post(
            settings.API_PATH + "/posts",
            headers=get_jwt_header(user),
            json={"channel_id": channel.id, "text": "hi"},
        )
        assert resp.status_code == 403, resp.text
        assert resp.json()["detail"]["error"] == "not_subscribed"

    async def test_create_fails_review_gate_locked(
        self, client: AsyncClient, db: AsyncSession, create_user, create_channel
    ):
        user: User = await create_user()
        channel: Channel = await create_channel()
        await subscribe(db, user, channel)

        resp = await client.post(
            settings.API_PATH + "/posts",
            headers=get_jwt_header(user),
            json={"channel_id": channel.id, "text": "hi"},
        )
        assert resp.status_code == 403, resp.text
        body = resp.json()["detail"]
        assert body["error"] == "review_gate_locked"
        assert body["reviewed_count"] == 0
        assert body["review_gate"] == settings.RELAY_REVIEW_GATE

    async def test_create_succeeds_when_gate_met(
        self, client: AsyncClient, db: AsyncSession, create_user, create_channel, create_post
    ):
        user: User = await create_user()
        channel: Channel = await create_channel()
        await subscribe(db, user, channel)
        for _ in range(settings.RELAY_REVIEW_GATE):
            post = await create_post(channel=channel)
            await review(db, user, post, "forward")

        resp = await client.post(
            settings.API_PATH + "/posts",
            headers=get_jwt_header(user),
            json={"channel_id": channel.id, "text": "unlocked post"},
        )
        assert resp.status_code == 201, resp.text
        assert resp.json()["text"] == "unlocked post"

    async def test_superuser_bypasses_review_gate(
        self, client: AsyncClient, db: AsyncSession, create_user, create_channel
    ):
        user: User = await create_user()
        user.is_superuser = True
        db.add(user)
        await db.commit()

        channel: Channel = await create_channel()
        await subscribe(db, user, channel)

        resp = await client.post(
            settings.API_PATH + "/posts",
            headers=get_jwt_header(user),
            json={"channel_id": channel.id, "text": "superuser post"},
        )
        assert resp.status_code == 201, resp.text


class TestGetPost:
    async def test_get_post_404_for_non_subscriber(
        self, client: AsyncClient, create_user, create_post
    ):
        user: User = await create_user()
        post: Post = await create_post()
        resp = await client.get(
            settings.API_PATH + f"/posts/{post.id}", headers=get_jwt_header(user)
        )
        assert resp.status_code == 404

    async def test_get_post_visible_to_author_even_if_anonymous(
        self, client: AsyncClient, db: AsyncSession, create_user, create_channel, create_post
    ):
        author: User = await create_user()
        channel: Channel = await create_channel()
        await subscribe(db, author, channel)
        post: Post = await create_post(channel=channel, author=author, is_anonymous=True)

        resp = await client.get(
            settings.API_PATH + f"/posts/{post.id}", headers=get_jwt_header(author)
        )
        assert resp.status_code == 200, resp.text
        assert resp.json()["author"]["id"] == str(author.id)


class TestReviewPost:
    async def test_review_updates_counters_and_returns_result(
        self, client: AsyncClient, db: AsyncSession, create_user, create_channel, create_post
    ):
        user: User = await create_user()
        channel: Channel = await create_channel()
        await subscribe(db, user, channel)
        post: Post = await create_post(channel=channel)

        resp = await client.post(
            settings.API_PATH + f"/posts/{post.id}/review",
            headers=get_jwt_header(user),
            json={"kind": "forward"},
        )
        assert resp.status_code == 200, resp.text
        data = resp.json()
        assert data["reviewed_count"] == 1
        assert data["review_gate"] == settings.RELAY_REVIEW_GATE
        assert data["unlocked"] is (1 >= settings.RELAY_REVIEW_GATE)

        await db.refresh(post)
        await db.refresh(user)
        assert post.forwarded_count == 1
        assert user.reviewed_count == 1
        assert user.forwarded_count == 1

    async def test_review_twice_is_conflict(
        self, client: AsyncClient, db: AsyncSession, create_user, create_channel, create_post
    ):
        user: User = await create_user()
        channel: Channel = await create_channel()
        await subscribe(db, user, channel)
        post: Post = await create_post(channel=channel)
        jwt_header = get_jwt_header(user)

        first = await client.post(
            settings.API_PATH + f"/posts/{post.id}/review",
            headers=jwt_header,
            json={"kind": "forward"},
        )
        assert first.status_code == 200, first.text

        second = await client.post(
            settings.API_PATH + f"/posts/{post.id}/review",
            headers=jwt_header,
            json={"kind": "drop"},
        )
        assert second.status_code == 409, second.text
        assert second.json()["detail"]["error"] == "already_reviewed"

    async def test_review_nonexistent_post(self, client: AsyncClient, create_user):
        user: User = await create_user()
        resp = await client.post(
            settings.API_PATH + f"/posts/{10**6}/review",
            headers=get_jwt_header(user),
            json={"kind": "forward"},
        )
        assert resp.status_code == 404
