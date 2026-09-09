import io
import json
import subprocess
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from httpx import AsyncClient
from PIL import Image
from redis.asyncio import Redis
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.feed import keys, service
from app.feed.worker import consume_once
from app.models.channel import Channel
from app.models.post import Post
from app.models.post_media import PostMedia
from app.models.post_review import PostReview
from app.models.user import User
from tests.utils import get_jwt_header, grant_subscription, review, subscribe


def _make_test_jpeg(
    width: int = 64,
    height: int = 48,
    *,
    with_exif: bool = False,
    orientation_tag: int | None = None,
) -> bytes:
    """A valid upload by default: 64x48 is 4:3, one of the two shapes
    POST_MEDIA_*_RATIO admits. Anything else here is deliberately the wrong shape.
    """
    img = Image.new("RGB", (width, height), color=(200, 50, 50))
    buf = io.BytesIO()
    if with_exif or orientation_tag is not None:
        exif = Image.Exif()
        exif[0x0112] = orientation_tag or 1  # Orientation - a plain, easy-to-set
        # representative tag; the real-world motivating case (see
        # media_validation.py) is JPEG GPS EXIF.
        img.save(buf, format="JPEG", exif=exif)
    else:
        img.save(buf, format="JPEG")
    return buf.getvalue()


def _make_test_png(width: int = 64, height: int = 48, *, alpha: bool = False) -> bytes:
    img = Image.new(
        "RGBA" if alpha else "RGB",
        (width, height),
        color=(200, 50, 50, 128) if alpha else (200, 50, 50),
    )
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


def _make_decompression_bomb_png() -> bytes:
    """A tiny-byte-count file with a declared pixel count well over
    2x Image.MAX_IMAGE_PIXELS (see media_validation.py) - solid color compresses to
    a few KB despite the huge declared dimensions."""
    img = Image.new("RGB", (12000, 12000), color=(0, 0, 0))
    buf = io.BytesIO()
    img.save(buf, format="PNG", optimize=True)
    return buf.getvalue()


def _make_test_video(
    duration: float = 1.0,
    *,
    with_metadata: bool = False,
    codec: str = "libx264",
    size: str = "64x64",
) -> bytes:
    with tempfile.TemporaryDirectory() as tmp_dir:
        out_path = Path(tmp_dir) / "clip.mp4"
        cmd = [
            "ffmpeg", "-y", "-f", "lavfi",
            "-i", f"testsrc=duration={duration}:size={size}:rate=5",
            "-c:v", codec,
            "-pix_fmt", "yuv420p",
        ]
        if with_metadata:
            cmd += ["-metadata", "location=+37.7749-122.4194/"]
        cmd += [str(out_path)]
        subprocess.run(cmd, check=True, capture_output=True)
        return out_path.read_bytes()


def _probe_video(data: bytes) -> dict:
    """format tags + the first video stream, of some stored/returned bytes."""
    with tempfile.TemporaryDirectory() as tmp_dir:
        path = Path(tmp_dir) / "clip.mp4"
        path.write_bytes(data)
        result = subprocess.run(
            [
                "ffprobe", "-v", "error",
                "-show_entries", "format_tags:format=format_name",
                "-show_entries", "stream=codec_name,codec_type,width,height",
                "-of", "json", str(path),
            ],
            check=True,
            capture_output=True,
        )
        parsed = json.loads(result.stdout)
        streams = parsed.get("streams", [])
        video = next((s for s in streams if s.get("codec_type") == "video"), {})
        return {
            "tags": parsed.get("format", {}).get("tags", {}),
            "video": video,
        }


def _probe_format_tags(data: bytes) -> dict:
    return _probe_video(data)["tags"]


def _text_block(text: str) -> dict:
    return {"type": "text", "text": text}


def _media_block(file_index: int, orientation: str | None = None) -> dict:
    block = {"type": "media", "file_index": file_index}
    if orientation is not None:
        block["orientation"] = orientation
    return block


def _blocks_json(*blocks: dict) -> str:
    """The `blocks` multipart form field `POST /posts` expects - a JSON-encoded
    list, since multipart has no native way to carry a nested list of objects."""
    return json.dumps(list(blocks))


def _media_blocks(post_json: dict) -> list[dict]:
    """The `media` sub-objects of every media-type block in a PostRead body, in
    order - the equivalent of the old flat `post["media"]` list."""
    return [b["media"] for b in post_json["blocks"] if b["type"] == "media"]


def _feed_posts(body: list[dict]) -> list[dict]:
    """The readable posts in a `GET /posts/feed` body.

    The route answers a list of `FeedEntry` envelopes rather than bare posts, so
    that a queue slot whose post has been erased can be reported as a hole rather
    than silently skipped (see TestFeedMissingPosts). Every test that only cares
    about the posts goes through here.
    """
    return [entry["post"] for entry in body if entry["post"] is not None]


class TestPostsFeed:
    async def test_feed_empty_with_empty_queue(
        self, client: AsyncClient, create_user
    ):
        user: User = await create_user()
        resp = await client.get(
            settings.API_PATH + "/posts/feed", headers=get_jwt_header(user)
        )
        assert resp.status_code == 200, resp.text
        assert resp.json() == []

    async def test_feed_returns_queued_posts(
        self,
        client: AsyncClient,
        redis: Redis,
        create_user,
        create_channel,
        create_post,
    ):
        user: User = await create_user()
        channel: Channel = await create_channel()
        post_a: Post = await create_post(channel=channel)
        post_b: Post = await create_post(channel=channel)
        await service.place_post(redis, str(user.id), post_a.id)
        await service.place_post(redis, str(user.id), post_b.id)

        resp = await client.get(
            settings.API_PATH + "/posts/feed", headers=get_jwt_header(user)
        )
        assert resp.status_code == 200, resp.text
        ids = [p["id"] for p in _feed_posts(resp.json())]
        # LPUSH ⇒ most recently placed is at the head.
        assert ids == [post_b.id, post_a.id]

    async def test_feed_anonymous_post_hides_author_for_other_users(
        self,
        client: AsyncClient,
        redis: Redis,
        create_user,
        create_channel,
        create_post,
    ):
        viewer: User = await create_user()
        channel: Channel = await create_channel()
        author: User = await create_user()
        post: Post = await create_post(channel=channel, author=author, is_anonymous=True)
        await service.place_post(redis, str(viewer.id), post.id)

        resp = await client.get(
            settings.API_PATH + "/posts/feed", headers=get_jwt_header(viewer)
        )
        assert resp.status_code == 200, resp.text
        [data] = [p for p in _feed_posts(resp.json()) if p["id"] == post.id]
        assert data["author"]["id"] is None
        assert data["author"]["username"] is None
        assert data["author"]["profile_picture_url"] is None

    async def test_feed_shows_authors_profile_picture(
        self,
        client: AsyncClient,
        media_client: AsyncClient,
        redis: Redis,
        create_user,
        create_channel,
        create_post,
    ):
        viewer: User = await create_user()
        channel: Channel = await create_channel()
        author: User = await create_user()
        await client.put(
            settings.API_PATH + "/users/me/profile-picture",
            files={"file": ("avatar.png", _make_test_png(), "image/png")},
            headers=get_jwt_header(author),
        )
        post: Post = await create_post(channel=channel, author=author)
        await service.place_post(redis, str(viewer.id), post.id)

        resp = await client.get(
            settings.API_PATH + "/posts/feed", headers=get_jwt_header(viewer)
        )
        assert resp.status_code == 200, resp.text
        [data] = [p for p in _feed_posts(resp.json()) if p["id"] == post.id]
        url = data["author"]["profile_picture_url"]
        assert url is not None
        # Presigned bucket URL, not a route back into this API - and it resolves
        # without the viewer's bearer token.
        assert url.startswith(str(settings.STORAGE_PUBLIC_ENDPOINT_URL))
        fetched = await media_client.get(url)
        assert fetched.status_code == 200, fetched.text
        # Re-encoded on upload, so compare what it decodes to, not raw bytes.
        # 64x48 is _make_test_png's default; an avatar has no shape rule, so the
        # source shape is preserved rather than squared off.
        assert Image.open(io.BytesIO(fetched.content)).size == (64, 48)

    async def test_feed_anonymous_post_hides_authors_profile_picture(
        self,
        client: AsyncClient,
        redis: Redis,
        create_user,
        create_channel,
        create_post,
    ):
        viewer: User = await create_user()
        channel: Channel = await create_channel()
        author: User = await create_user()
        await client.put(
            settings.API_PATH + "/users/me/profile-picture",
            files={"file": ("avatar.png", _make_test_png(), "image/png")},
            headers=get_jwt_header(author),
        )
        post: Post = await create_post(channel=channel, author=author, is_anonymous=True)
        await service.place_post(redis, str(viewer.id), post.id)

        resp = await client.get(
            settings.API_PATH + "/posts/feed", headers=get_jwt_header(viewer)
        )
        assert resp.status_code == 200, resp.text
        [data] = [p for p in _feed_posts(resp.json()) if p["id"] == post.id]
        assert data["author"]["profile_picture_url"] is None

    async def test_feed_filtered_by_channel_id(
        self,
        client: AsyncClient,
        redis: Redis,
        create_user,
        create_channel,
        create_post,
    ):
        user: User = await create_user()
        channel_a: Channel = await create_channel()
        channel_b: Channel = await create_channel()
        post_a: Post = await create_post(channel=channel_a)
        post_b: Post = await create_post(channel=channel_b)
        await service.place_post(redis, str(user.id), post_a.id)
        await service.place_post(redis, str(user.id), post_b.id)

        resp = await client.get(
            settings.API_PATH + "/posts/feed",
            params={"channel_id": channel_a.id},
            headers=get_jwt_header(user),
        )
        assert resp.status_code == 200, resp.text
        ids = [p["id"] for p in _feed_posts(resp.json())]
        assert ids == [post_a.id]

    async def test_feed_channel_filter_pages_the_filtered_result(
        self,
        client: AsyncClient,
        redis: Redis,
        create_user,
        create_channel,
        create_post,
    ):
        """A channel filter must be applied before `skip`/`limit`, not after.

        Filtering a page would make `limit=1` return nothing whenever the head of
        the queue belongs to another channel, and leave the rest of that channel
        unreachable at every offset.
        """
        user: User = await create_user()
        channel_a: Channel = await create_channel()
        channel_b: Channel = await create_channel()
        wanted = [await create_post(channel=channel_a) for _ in range(2)]
        noise = await create_post(channel=channel_b)
        for post in (*wanted, noise):
            await service.place_post(redis, str(user.id), post.id)

        # `noise` was placed last, so it sits at the head of the queue (LPUSH).
        resp = await client.get(
            settings.API_PATH + "/posts/feed",
            params={"channel_id": channel_a.id, "limit": 1},
            headers=get_jwt_header(user),
        )
        assert resp.status_code == 200, resp.text
        assert [p["id"] for p in _feed_posts(resp.json())] == [wanted[1].id]

        resp = await client.get(
            settings.API_PATH + "/posts/feed",
            params={"channel_id": channel_a.id, "skip": 1, "limit": 1},
            headers=get_jwt_header(user),
        )
        assert resp.status_code == 200, resp.text
        assert [p["id"] for p in _feed_posts(resp.json())] == [wanted[0].id]


class TestFeedMissingPosts:
    """A queue slot whose post no longer exists - what an author erasing their
    account leaves behind in everybody else's queue (app/core/account_deletion.py).

    Nothing goes looking for those ids at deletion time; the feed is where they
    are noticed, and the reader is given a way to clear the slot.
    """

    async def test_feed_reports_the_hole_rather_than_skipping_it(
        self, client: AsyncClient, db: AsyncSession, redis: Redis,
        create_user, create_channel, create_post,
    ):
        user: User = await create_user()
        channel: Channel = await create_channel()
        alive: Post = await create_post(channel=channel)
        doomed: Post = await create_post(channel=channel)
        await service.place_post(redis, str(user.id), alive.id)
        await service.place_post(redis, str(user.id), doomed.id)
        await db.delete(doomed)
        await db.commit()

        resp = await client.get(
            settings.API_PATH + "/posts/feed", headers=get_jwt_header(user)
        )
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert [e["post_id"] for e in body] == [doomed.id, alive.id]
        assert body[0]["post"] is None
        assert body[1]["post"]["id"] == alive.id

    async def test_a_channel_filter_hides_holes(
        self, client: AsyncClient, db: AsyncSession, redis: Redis,
        create_user, create_channel, create_post,
    ):
        """An erased post has no channel left, so it cannot honestly be claimed
        for the one being filtered on - and 'not in this channel' and 'gone' are
        indistinguishable once the row is missing."""
        user: User = await create_user()
        channel: Channel = await create_channel()
        alive: Post = await create_post(channel=channel)
        doomed: Post = await create_post(channel=channel)
        await service.place_post(redis, str(user.id), alive.id)
        await service.place_post(redis, str(user.id), doomed.id)
        await db.delete(doomed)
        await db.commit()

        resp = await client.get(
            settings.API_PATH + "/posts/feed",
            params={"channel_id": channel.id},
            headers=get_jwt_header(user),
        )
        assert resp.status_code == 200, resp.text
        assert [e["post_id"] for e in resp.json()] == [alive.id]

    async def test_dismissing_frees_the_slot(
        self, client: AsyncClient, db: AsyncSession, redis: Redis,
        create_user, create_channel, create_post,
    ):
        user: User = await create_user()
        channel: Channel = await create_channel()
        doomed: Post = await create_post(channel=channel)
        await service.place_post(redis, str(user.id), doomed.id)
        await db.delete(doomed)
        await db.commit()

        resp = await client.delete(
            settings.API_PATH + f"/posts/feed/{doomed.id}",
            headers=get_jwt_header(user),
        )
        assert resp.status_code == 204, resp.text
        assert await service.render_queue_ids(redis, str(user.id), 20) == []
        # The slot is genuinely back: dismissing has to make the reader reachable
        # by fan-out again, exactly as a review does.
        assert await redis.sismember(keys.FREE_QUEUE, str(user.id)) == 1

    async def test_dismissing_earns_nothing_and_records_no_review(
        self, client: AsyncClient, db: AsyncSession, redis: Redis,
        create_user, create_channel, create_post,
    ):
        """Not a verdict: nobody read anything, and a PostReview row would point
        at a post that is gone."""
        user: User = await create_user()
        channel: Channel = await create_channel()
        doomed: Post = await create_post(channel=channel)
        await service.place_post(redis, str(user.id), doomed.id)
        await db.delete(doomed)
        await db.commit()
        before = await service.token_balance(redis, str(user.id))

        resp = await client.delete(
            settings.API_PATH + f"/posts/feed/{doomed.id}",
            headers=get_jwt_header(user),
        )
        assert resp.status_code == 204, resp.text
        assert await service.token_balance(redis, str(user.id)) == before
        assert await db.scalar(
            select(func.count()).select_from(PostReview).where(
                PostReview.user_id == user.id
            )
        ) == 0

    async def test_refused_while_the_post_still_exists(
        self, client: AsyncClient, redis: Redis, create_user, create_channel,
        create_post,
    ):
        """Otherwise this is a way to clear a post without judging it."""
        user: User = await create_user()
        channel: Channel = await create_channel()
        post: Post = await create_post(channel=channel)
        await service.place_post(redis, str(user.id), post.id)

        resp = await client.delete(
            settings.API_PATH + f"/posts/feed/{post.id}",
            headers=get_jwt_header(user),
        )
        assert resp.status_code == 409, resp.text
        assert resp.json()["detail"]["error"] == "post_available"
        assert await service.render_queue_ids(redis, str(user.id), 20) == [post.id]

    async def test_refused_when_it_was_never_queued(
        self, client: AsyncClient, db: AsyncSession, create_user, create_channel,
        create_post,
    ):
        """A deleted post nobody was holding is not this reader's slot to reclaim."""
        user: User = await create_user()
        channel: Channel = await create_channel()
        doomed: Post = await create_post(channel=channel)
        post_id = doomed.id
        await db.delete(doomed)
        await db.commit()

        resp = await client.delete(
            settings.API_PATH + f"/posts/feed/{post_id}",
            headers=get_jwt_header(user),
        )
        assert resp.status_code == 409, resp.text
        assert resp.json()["detail"]["error"] == "not_in_queue"


class TestFeedStatus:
    async def test_status_empty_queue(self, client: AsyncClient, create_user):
        user: User = await create_user()
        resp = await client.get(
            settings.API_PATH + "/posts/feed/status", headers=get_jwt_header(user)
        )
        assert resp.status_code == 200, resp.text
        assert resp.json() == {
            "post_ids": [],
            "capacity": settings.FEED_QUEUE_MAX_SLOTS,
        }

    async def test_status_lists_the_queue_in_feed_order(
        self,
        client: AsyncClient,
        redis: Redis,
        create_user,
        create_channel,
        create_post,
    ):
        user: User = await create_user()
        channel: Channel = await create_channel()
        post_a: Post = await create_post(channel=channel)
        post_b: Post = await create_post(channel=channel)
        await service.place_post(redis, str(user.id), post_a.id)
        await service.place_post(redis, str(user.id), post_b.id)

        resp = await client.get(
            settings.API_PATH + "/posts/feed/status", headers=get_jwt_header(user)
        )
        assert resp.status_code == 200, resp.text
        # Same order the feed itself renders, so a client can compare id-for-id.
        assert resp.json()["post_ids"] == [post_b.id, post_a.id]

    async def test_status_requires_authentication(self, client: AsyncClient):
        resp = await client.get(settings.API_PATH + "/posts/feed/status")
        assert resp.status_code == 401, resp.text


class TestCreatePost:
    async def test_create_fails_insufficient_tokens(
        self, client: AsyncClient, create_user, create_channel
    ):
        user: User = await create_user()  # starts with 0 tokens
        channel: Channel = await create_channel()

        resp = await client.post(
            settings.API_PATH + "/posts",
            headers=get_jwt_header(user),
            data={"channel_id": channel.id, "blocks": _blocks_json(_text_block("hi"))},
        )
        assert resp.status_code == 402, resp.text
        body = resp.json()["detail"]
        assert body["error"] == "insufficient_tokens"
        assert body["balance"] == 0
        assert body["price"] >= settings.FEED_PRICE_MIN

    async def test_create_succeeds_with_tokens_and_enqueues_op(
        self, client: AsyncClient, redis: Redis, create_user, create_channel
    ):
        user: User = await create_user()
        channel: Channel = await create_channel()
        await service.earn_token(redis, str(user.id), settings.FEED_PRICE_MAX)

        resp = await client.post(
            settings.API_PATH + "/posts",
            headers=get_jwt_header(user),
            data={
                "channel_id": channel.id,
                "blocks": _blocks_json(_text_block("unlocked post")),
            },
        )
        assert resp.status_code == 201, resp.text
        data = resp.json()
        assert data["post"]["blocks"] == [
            {"type": "text", "text": "unlocked post", "media": None}
        ]
        assert data["price"] >= settings.FEED_PRICE_MIN
        assert data["token_balance"] == settings.FEED_PRICE_MAX - data["price"]
        # The new post is queued as an operation for the worker to distribute.
        assert await service.operation_queue_len(redis) == 1

    async def test_superuser_posts_for_free(
        self, client: AsyncClient, db: AsyncSession, create_user, create_channel
    ):
        user: User = await create_user()
        user.is_superuser = True
        db.add(user)
        await db.commit()

        channel: Channel = await create_channel()
        resp = await client.post(
            settings.API_PATH + "/posts",
            headers=get_jwt_header(user),
            data={
                "channel_id": channel.id,
                "blocks": _blocks_json(_text_block("superuser post")),
            },
        )
        assert resp.status_code == 201, resp.text
        assert resp.json()["token_balance"] == 0  # nothing spent, nothing earned

    async def test_create_channel_404(
        self, client: AsyncClient, redis: Redis, create_user
    ):
        user: User = await create_user()
        await service.earn_token(redis, str(user.id), 10)
        resp = await client.post(
            settings.API_PATH + "/posts",
            headers=get_jwt_header(user),
            data={"channel_id": 10**6, "blocks": _blocks_json(_text_block("hi"))},
        )
        assert resp.status_code == 404

    async def test_create_post_snapshots_supporter_subscription(
        self, client: AsyncClient, redis: Redis, db: AsyncSession, create_user, create_channel
    ):
        user: User = await create_user()
        channel: Channel = await create_channel()
        await service.earn_token(redis, str(user.id), settings.FEED_PRICE_MAX)
        await grant_subscription(db, user, "supporter")

        resp = await client.post(
            settings.API_PATH + "/posts",
            headers=get_jwt_header(user),
            data={
                "channel_id": channel.id,
                "blocks": _blocks_json(_text_block("supporter post")),
            },
        )
        assert resp.status_code == 201, resp.text
        assert resp.json()["post"]["subscription_kind"] == "supporter"

    async def test_create_post_without_subscription_has_no_badge(
        self, client: AsyncClient, redis: Redis, create_user, create_channel
    ):
        user: User = await create_user()
        channel: Channel = await create_channel()
        await service.earn_token(redis, str(user.id), settings.FEED_PRICE_MAX)

        resp = await client.post(
            settings.API_PATH + "/posts",
            headers=get_jwt_header(user),
            data={
                "channel_id": channel.id,
                "blocks": _blocks_json(_text_block("free post")),
            },
        )
        assert resp.status_code == 201, resp.text
        assert resp.json()["post"]["subscription_kind"] is None

    async def test_post_keeps_supporter_badge_after_subscription_revoked(
        self, client: AsyncClient, redis: Redis, db: AsyncSession, create_user, create_channel
    ):
        user: User = await create_user()
        channel: Channel = await create_channel()
        await subscribe(db, user, channel)
        await service.earn_token(redis, str(user.id), settings.FEED_PRICE_MAX)
        subscription = await grant_subscription(db, user, "supporter")

        resp = await client.post(
            settings.API_PATH + "/posts",
            headers=get_jwt_header(user),
            data={
                "channel_id": channel.id,
                "blocks": _blocks_json(_text_block("supporter post")),
            },
        )
        assert resp.status_code == 201, resp.text
        post_id = resp.json()["post"]["id"]

        await db.delete(subscription)
        await db.commit()

        resp = await client.get(
            settings.API_PATH + f"/posts/{post_id}",
            headers=get_jwt_header(user),
        )
        assert resp.status_code == 200, resp.text
        assert resp.json()["subscription_kind"] == "supporter"


class TestCreatePostMedia:
    async def test_image_round_trips_with_reencoded_content_type(
        self, client: AsyncClient, media_client: AsyncClient, redis: Redis, create_user, create_channel
    ):
        user: User = await create_user()
        channel: Channel = await create_channel()
        await service.earn_token(redis, str(user.id), settings.FEED_PRICE_MAX)

        resp = await client.post(
            settings.API_PATH + "/posts",
            headers=get_jwt_header(user),
            data={
                "channel_id": channel.id,
                "blocks": _blocks_json(_text_block("with a photo"), _media_block(0)),
            },
            files=[("files", ("photo.jpg", _make_test_jpeg(), "image/jpeg"))],
        )
        assert resp.status_code == 201, resp.text
        [media] = _media_blocks(resp.json()["post"])
        assert media["media_type"] == "image"
        assert media["content_type"] == "image/jpeg"

        fetched = await media_client.get(media["url"])
        assert fetched.status_code == 200, fetched.text
        assert fetched.headers["content-type"] == "image/jpeg"
        assert Image.open(io.BytesIO(fetched.content)).format == "JPEG"

    async def test_image_exif_is_stripped(
        self, client: AsyncClient, media_client: AsyncClient, redis: Redis, create_user, create_channel
    ):
        user: User = await create_user()
        channel: Channel = await create_channel()
        await service.earn_token(redis, str(user.id), settings.FEED_PRICE_MAX)

        resp = await client.post(
            settings.API_PATH + "/posts",
            headers=get_jwt_header(user),
            data={
                "channel_id": channel.id,
                "blocks": _blocks_json(_text_block("tagged"), _media_block(0)),
            },
            files=[
                ("files", ("photo.jpg", _make_test_jpeg(with_exif=True), "image/jpeg"))
            ],
        )
        assert resp.status_code == 201, resp.text
        url = _media_blocks(resp.json()["post"])[0]["url"]

        fetched = await media_client.get(url)
        stored = Image.open(io.BytesIO(fetched.content))
        assert stored.getexif() == {}

    async def test_video_round_trips_with_measured_duration_and_stripped_metadata(
        self, client: AsyncClient, media_client: AsyncClient, redis: Redis, create_user, create_channel
    ):
        user: User = await create_user()
        channel: Channel = await create_channel()
        await service.earn_token(redis, str(user.id), settings.FEED_PRICE_MAX)
        clip = _make_test_video(duration=1.0, with_metadata=True)
        assert _probe_format_tags(clip)  # sanity: the fixture actually has metadata

        resp = await client.post(
            settings.API_PATH + "/posts",
            headers=get_jwt_header(user),
            data={
                "channel_id": channel.id,
                "blocks": _blocks_json(_text_block("with a clip"), _media_block(0)),
            },
            files=[("files", ("clip.mp4", clip, "video/mp4"))],
        )
        assert resp.status_code == 201, resp.text
        media = _media_blocks(resp.json()["post"])[0]
        assert media["media_type"] == "video"
        assert media["duration_seconds"] == pytest.approx(1.0, abs=0.5)

        fetched = await media_client.get(media["url"])
        assert fetched.status_code == 200, fetched.text
        # The transcode strips the location tag; ffmpeg still writes a handful of
        # structural container tags (major_brand, encoder, ...) that carry no
        # user data - only "location" is what this test is about.
        assert "location" not in _probe_format_tags(fetched.content)

    async def test_hevc_upload_is_transcoded_to_h264(
        self, client: AsyncClient, media_client: AsyncClient, redis: Redis, create_user, create_channel
    ):
        """The regression that made video unplayable in every browser: phones
        default to HEVC, which no Chromium-based browser can decode, and the
        old `-c copy` remux stored it unchanged. Such a clip parses far enough
        to report a duration and then decodes to a 0x0 picture - so asserting
        "it round-trips" is not enough, the stored codec itself has to be H.264.
        """
        user: User = await create_user()
        channel: Channel = await create_channel()
        await service.earn_token(redis, str(user.id), settings.FEED_PRICE_MAX)
        clip = _make_test_video(duration=1.0, codec="libx265")
        assert _probe_video(clip)["video"]["codec_name"] == "hevc"  # sanity

        resp = await client.post(
            settings.API_PATH + "/posts",
            headers=get_jwt_header(user),
            data={
                "channel_id": channel.id,
                "blocks": _blocks_json(_media_block(0)),
            },
            files=[("files", ("clip.mp4", clip, "video/mp4"))],
        )
        assert resp.status_code == 201, resp.text
        media = _media_blocks(resp.json()["post"])[0]
        assert media["content_type"] == "video/mp4"

        fetched = await media_client.get(media["url"])
        assert fetched.status_code == 200, fetched.text
        assert _probe_video(fetched.content)["video"]["codec_name"] == "h264"

    async def test_oversized_video_is_scaled_down(
        self, client: AsyncClient, media_client: AsyncClient, redis: Redis, create_user, create_channel
    ):
        """Output is bounded by POST_VIDEO_MAX_DIMENSION_PX - which is what keeps
        stored objects a sane size for high-bitrate phone footage."""
        user: User = await create_user()
        channel: Channel = await create_channel()
        await service.earn_token(redis, str(user.id), settings.FEED_PRICE_MAX)
        clip = _make_test_video(duration=1.0, size="1920x1080")

        resp = await client.post(
            settings.API_PATH + "/posts",
            headers=get_jwt_header(user),
            data={
                "channel_id": channel.id,
                "blocks": _blocks_json(_media_block(0)),
            },
            files=[("files", ("clip.mp4", clip, "video/mp4"))],
        )
        assert resp.status_code == 201, resp.text
        url = _media_blocks(resp.json()["post"])[0]["url"]

        fetched = await media_client.get(url)
        video = _probe_video(fetched.content)["video"]
        assert video["width"] == settings.POST_VIDEO_MAX_DIMENSION_PX
        assert video["height"] <= settings.POST_VIDEO_MAX_DIMENSION_PX
        # Even dimensions, which H.264 requires.
        assert video["width"] % 2 == 0 and video["height"] % 2 == 0

    async def test_image_reports_its_dimensions(
        self, client: AsyncClient, redis: Redis, create_user, create_channel
    ):
        """`width`/`height` are what lets a client reserve the right box for a
        media block before the bytes arrive - see PostMediaRead."""
        user: User = await create_user()
        channel: Channel = await create_channel()
        await service.earn_token(redis, str(user.id), settings.FEED_PRICE_MAX)

        resp = await client.post(
            settings.API_PATH + "/posts",
            headers=get_jwt_header(user),
            data={
                "channel_id": channel.id,
                "blocks": _blocks_json(_media_block(0)),
            },
            files=[("files", ("photo.jpg", _make_test_jpeg(400, 300), "image/jpeg"))],
        )
        assert resp.status_code == 201, resp.text
        media = _media_blocks(resp.json()["post"])[0]
        assert (media["width"], media["height"]) == (400, 300)
        assert media["poster_url"] is None  # images are their own preview

    async def test_portrait_image_accepted(
        self, client: AsyncClient, redis: Redis, create_user, create_channel
    ):
        user: User = await create_user()
        channel: Channel = await create_channel()
        await service.earn_token(redis, str(user.id), settings.FEED_PRICE_MAX)

        resp = await client.post(
            settings.API_PATH + "/posts",
            headers=get_jwt_header(user),
            data={
                "channel_id": channel.id,
                "blocks": _blocks_json(_media_block(0)),
            },
            files=[("files", ("photo.jpg", _make_test_jpeg(80, 100), "image/jpeg"))],
        )
        assert resp.status_code == 201, resp.text

    async def test_image_with_disallowed_aspect_ratio_rejected(
        self, client: AsyncClient, db: AsyncSession, redis: Redis, create_user, create_channel
    ):
        """Images are cropped in the client, which owns the only UI that can show
        the author what the crop discards - so an odd shape arriving here means
        that step was skipped, and is an error rather than something to guess at.
        """
        user: User = await create_user()
        channel: Channel = await create_channel()
        await service.earn_token(redis, str(user.id), settings.FEED_PRICE_MAX)
        balance_before = await service.token_balance(redis, str(user.id))
        posts_before = await db.scalar(select(func.count()).select_from(Post))

        resp = await client.post(
            settings.API_PATH + "/posts",
            headers=get_jwt_header(user),
            data={
                "channel_id": channel.id,
                "blocks": _blocks_json(_media_block(0)),
            },
            files=[("files", ("square.jpg", _make_test_jpeg(64, 64), "image/jpeg"))],
        )
        assert resp.status_code == 400, resp.text
        assert resp.json()["detail"]["error"] == "post_media_invalid_aspect_ratio"
        # Validation runs before the charge, like every other media rejection.
        assert await service.token_balance(redis, str(user.id)) == balance_before
        assert await db.scalar(select(func.count()).select_from(Post)) == posts_before

    async def test_exif_rotation_is_applied_not_just_stripped(
        self, client: AsyncClient, media_client: AsyncClient, redis: Redis, create_user, create_channel
    ):
        """A phone "portrait" photo is often a landscape sensor frame plus a
        rotate-90 EXIF tag. Since this backend strips EXIF, not applying the
        rotation first would store the picture sideways *and* measure it against
        the wrong ratio - so the stored bytes must come back with the axes swapped.
        """
        user: User = await create_user()
        channel: Channel = await create_channel()
        await service.earn_token(redis, str(user.id), settings.FEED_PRICE_MAX)
        # 100x80 stored + "rotate 90 CW" => 80x100 as displayed, which is 4:5.
        photo = _make_test_jpeg(100, 80, orientation_tag=6)

        resp = await client.post(
            settings.API_PATH + "/posts",
            headers=get_jwt_header(user),
            data={
                "channel_id": channel.id,
                "blocks": _blocks_json(_media_block(0)),
            },
            files=[("files", ("rotated.jpg", photo, "image/jpeg"))],
        )
        assert resp.status_code == 201, resp.text
        media = _media_blocks(resp.json()["post"])[0]
        assert (media["width"], media["height"]) == (80, 100)

        fetched = await media_client.get(media["url"])
        assert Image.open(io.BytesIO(fetched.content)).size == (80, 100)

    async def test_opaque_png_is_stored_as_jpeg_but_transparency_survives(
        self, client: AsyncClient, redis: Redis, create_user, create_channel
    ):
        """The client's cropper renders through a canvas and can only hand back
        PNG, which is ~10x the bytes of a JPEG for a photo - paid for in every
        row and every fetch, since media lives in Postgres. So an opaque PNG is
        re-encoded, and only actual transparency keeps the heavier format.
        """
        user: User = await create_user()
        channel: Channel = await create_channel()
        await service.earn_token(redis, str(user.id), settings.FEED_PRICE_MAX * 2)

        resp = await client.post(
            settings.API_PATH + "/posts",
            headers=get_jwt_header(user),
            data={
                "channel_id": channel.id,
                "blocks": _blocks_json(_media_block(0), _media_block(1)),
            },
            files=[
                ("files", ("opaque.png", _make_test_png(), "image/png")),
                ("files", ("alpha.png", _make_test_png(alpha=True), "image/png")),
            ],
        )
        assert resp.status_code == 201, resp.text
        opaque, alpha = _media_blocks(resp.json()["post"])
        assert opaque["content_type"] == "image/jpeg"
        assert alpha["content_type"] == "image/png"

    async def test_video_is_cropped_to_the_chosen_orientation(
        self, client: AsyncClient, media_client: AsyncClient, redis: Redis, create_user, create_channel
    ):
        """A Flutter client cannot re-encode video, so unlike an image it sends
        only an orientation and the center crop happens server-side, inside the
        transcode that was running anyway. A landscape source asked for portrait
        must come back portrait.
        """
        user: User = await create_user()
        channel: Channel = await create_channel()
        await service.earn_token(redis, str(user.id), settings.FEED_PRICE_MAX)
        clip = _make_test_video(duration=1.0, size="640x360")

        resp = await client.post(
            settings.API_PATH + "/posts",
            headers=get_jwt_header(user),
            data={
                "channel_id": channel.id,
                "blocks": _blocks_json(_media_block(0, orientation="portrait")),
            },
            files=[("files", ("clip.mp4", clip, "video/mp4"))],
        )
        assert resp.status_code == 201, resp.text
        media = _media_blocks(resp.json()["post"])[0]
        ratio = media["width"] / media["height"]
        assert ratio == pytest.approx(settings.POST_MEDIA_PORTRAIT_RATIO, rel=0.03)

        fetched = await media_client.get(media["url"])
        video = _probe_video(fetched.content)["video"]
        assert (video["width"], video["height"]) == (media["width"], media["height"])

    async def test_video_without_orientation_falls_back_to_nearest_shape(
        self, client: AsyncClient, redis: Redis, create_user, create_channel
    ):
        user: User = await create_user()
        channel: Channel = await create_channel()
        await service.earn_token(redis, str(user.id), settings.FEED_PRICE_MAX)
        clip = _make_test_video(duration=1.0, size="640x360")  # 16:9, clearly landscape

        resp = await client.post(
            settings.API_PATH + "/posts",
            headers=get_jwt_header(user),
            data={
                "channel_id": channel.id,
                "blocks": _blocks_json(_media_block(0)),
            },
            files=[("files", ("clip.mp4", clip, "video/mp4"))],
        )
        assert resp.status_code == 201, resp.text
        media = _media_blocks(resp.json()["post"])[0]
        ratio = media["width"] / media["height"]
        assert ratio == pytest.approx(settings.POST_MEDIA_LANDSCAPE_RATIO, rel=0.03)

    async def test_video_poster_is_served_at_the_clips_aspect_ratio(
        self, client: AsyncClient, media_client: AsyncClient, redis: Redis, create_user, create_channel
    ):
        """The poster is what makes an unplayed video look like a photo instead of
        a black rectangle, so it has to be a real frame *of that clip* - same
        shape, fetchable on its own without touching the video bytes."""
        user: User = await create_user()
        channel: Channel = await create_channel()
        await service.earn_token(redis, str(user.id), settings.FEED_PRICE_MAX)
        clip = _make_test_video(duration=2.0, size="640x360")

        resp = await client.post(
            settings.API_PATH + "/posts",
            headers=get_jwt_header(user),
            data={
                "channel_id": channel.id,
                "blocks": _blocks_json(_media_block(0, orientation="landscape")),
            },
            files=[("files", ("clip.mp4", clip, "video/mp4"))],
        )
        assert resp.status_code == 201, resp.text
        media = _media_blocks(resp.json()["post"])[0]
        assert media["poster_url"] is not None

        fetched = await media_client.get(media["poster_url"])
        assert fetched.status_code == 200, fetched.text
        assert fetched.headers["content-type"] == "image/jpeg"
        poster = Image.open(io.BytesIO(fetched.content))
        assert poster.format == "JPEG"
        assert poster.width / poster.height == pytest.approx(
            media["width"] / media["height"], rel=0.03
        )

    async def test_video_poster_is_gated_like_the_post_itself(
        self, client: AsyncClient, redis: Redis, create_user, create_channel
    ):
        """A poster is a frame of the video, so a looser gate than the post's own
        would leak the content of a post the viewer can't open.

        With presigned URLs the gate is enforced one step earlier than it used to
        be: a viewer who cannot open the post is never handed the poster's URL in
        the first place, so there is nothing for them to fetch."""
        author: User = await create_user()
        stranger: User = await create_user()
        channel: Channel = await create_channel()
        await service.earn_token(redis, str(author.id), settings.FEED_PRICE_MAX)

        resp = await client.post(
            settings.API_PATH + "/posts",
            headers=get_jwt_header(author),
            data={
                "channel_id": channel.id,
                "blocks": _blocks_json(_media_block(0)),
            },
            files=[("files", ("clip.mp4", _make_test_video(duration=1.0), "video/mp4"))],
        )
        assert resp.status_code == 201, resp.text
        post_id = resp.json()["post"]["id"]
        assert _media_blocks(resp.json()["post"])[0]["poster_url"] is not None

        denied = await client.get(
            settings.API_PATH + f"/posts/{post_id}", headers=get_jwt_header(stranger)
        )
        assert denied.status_code == 404, denied.text

    async def test_small_video_is_not_upscaled(
        self, client: AsyncClient, redis: Redis, create_user, create_channel
    ):
        """`scale` with a constant bounding box happily enlarges a small clip,
        spending bitrate and stored bytes on invented pixels."""
        user: User = await create_user()
        channel: Channel = await create_channel()
        await service.earn_token(redis, str(user.id), settings.FEED_PRICE_MAX)

        resp = await client.post(
            settings.API_PATH + "/posts",
            headers=get_jwt_header(user),
            data={
                "channel_id": channel.id,
                "blocks": _blocks_json(_media_block(0, orientation="landscape")),
            },
            files=[
                (
                    "files",
                    ("clip.mp4", _make_test_video(duration=1.0, size="160x120"), "video/mp4"),
                )
            ],
        )
        assert resp.status_code == 201, resp.text
        media = _media_blocks(resp.json()["post"])[0]
        assert media["width"] <= 160 and media["height"] <= 120

    async def test_too_many_files_rejected_before_any_charge_or_post(
        self, client: AsyncClient, db: AsyncSession, redis: Redis, create_user, create_channel
    ):
        user: User = await create_user()
        channel: Channel = await create_channel()
        await service.earn_token(redis, str(user.id), settings.FEED_PRICE_MAX)
        balance_before = await service.token_balance(redis, str(user.id))
        posts_before = await db.scalar(select(func.count()).select_from(Post))

        count = settings.POST_MEDIA_MAX_FILES + 1
        files = [
            ("files", (f"p{i}.jpg", _make_test_jpeg(), "image/jpeg"))
            for i in range(count)
        ]
        resp = await client.post(
            settings.API_PATH + "/posts",
            headers=get_jwt_header(user),
            data={
                "channel_id": channel.id,
                "blocks": _blocks_json(
                    _text_block("too many"),
                    *[_media_block(i) for i in range(count)],
                ),
            },
            files=files,
        )
        assert resp.status_code == 400, resp.text
        assert resp.json()["detail"]["error"] == "post_media_too_many_files"
        assert await service.token_balance(redis, str(user.id)) == balance_before
        assert await db.scalar(select(func.count()).select_from(Post)) == posts_before

    async def test_oversized_image_rejected(
        self, client: AsyncClient, redis: Redis, create_user, create_channel, monkeypatch
    ):
        monkeypatch.setattr(settings, "POST_IMAGE_MAX_BYTES", 10)
        user: User = await create_user()
        channel: Channel = await create_channel()
        await service.earn_token(redis, str(user.id), settings.FEED_PRICE_MAX)

        resp = await client.post(
            settings.API_PATH + "/posts",
            headers=get_jwt_header(user),
            data={
                "channel_id": channel.id,
                "blocks": _blocks_json(_text_block("big"), _media_block(0)),
            },
            files=[("files", ("photo.jpg", _make_test_jpeg(), "image/jpeg"))],
        )
        assert resp.status_code == 400, resp.text
        assert resp.json()["detail"]["error"] == "post_media_too_large"

    async def test_video_over_duration_cap_rejected(
        self, client: AsyncClient, redis: Redis, create_user, create_channel, monkeypatch
    ):
        monkeypatch.setattr(settings, "POST_VIDEO_MAX_DURATION_SECONDS", 1)
        user: User = await create_user()
        channel: Channel = await create_channel()
        await service.earn_token(redis, str(user.id), settings.FEED_PRICE_MAX)
        clip = _make_test_video(duration=3.0)

        resp = await client.post(
            settings.API_PATH + "/posts",
            headers=get_jwt_header(user),
            data={
                "channel_id": channel.id,
                "blocks": _blocks_json(_text_block("long clip"), _media_block(0)),
            },
            files=[("files", ("clip.mp4", clip, "video/mp4"))],
        )
        assert resp.status_code == 400, resp.text
        assert resp.json()["detail"]["error"] == "post_media_video_too_long"

    async def test_wrong_content_type_rejected(
        self, client: AsyncClient, redis: Redis, create_user, create_channel
    ):
        user: User = await create_user()
        channel: Channel = await create_channel()
        await service.earn_token(redis, str(user.id), settings.FEED_PRICE_MAX)

        resp = await client.post(
            settings.API_PATH + "/posts",
            headers=get_jwt_header(user),
            data={
                "channel_id": channel.id,
                "blocks": _blocks_json(_text_block("not an image"), _media_block(0)),
            },
            files=[("files", ("notes.txt", b"just text", "text/plain"))],
        )
        assert resp.status_code == 400, resp.text
        assert resp.json()["detail"]["error"] == "post_media_invalid_type"

    async def test_bytes_lying_about_content_type_rejected(
        self, client: AsyncClient, redis: Redis, create_user, create_channel
    ):
        """The client-declared content_type is only used to pick image vs. video
        handling - Pillow's own parse of the bytes is what actually decides."""
        user: User = await create_user()
        channel: Channel = await create_channel()
        await service.earn_token(redis, str(user.id), settings.FEED_PRICE_MAX)

        resp = await client.post(
            settings.API_PATH + "/posts",
            headers=get_jwt_header(user),
            data={
                "channel_id": channel.id,
                "blocks": _blocks_json(_text_block("fake"), _media_block(0)),
            },
            files=[("files", ("fake.jpg", b"not-really-a-jpeg", "image/jpeg"))],
        )
        assert resp.status_code == 400, resp.text
        assert resp.json()["detail"]["error"] == "post_media_invalid_type"

    async def test_combined_total_over_cap_rejected(
        self, client: AsyncClient, redis: Redis, create_user, create_channel, monkeypatch
    ):
        monkeypatch.setattr(settings, "POST_MEDIA_MAX_TOTAL_BYTES", 1000)
        user: User = await create_user()
        channel: Channel = await create_channel()
        await service.earn_token(redis, str(user.id), settings.FEED_PRICE_MAX)
        # Each individually under POST_IMAGE_MAX_BYTES, but combined over the cap.
        files = [
            ("files", ("a.jpg", _make_test_jpeg(400, 300), "image/jpeg")),
            ("files", ("b.jpg", _make_test_jpeg(400, 300), "image/jpeg")),
        ]

        resp = await client.post(
            settings.API_PATH + "/posts",
            headers=get_jwt_header(user),
            data={
                "channel_id": channel.id,
                "blocks": _blocks_json(
                    _text_block("two photos"), _media_block(0), _media_block(1)
                ),
            },
            files=files,
        )
        assert resp.status_code == 400, resp.text
        assert resp.json()["detail"]["error"] == "post_media_total_too_large"

    async def test_decompression_bomb_rejected(
        self, client: AsyncClient, redis: Redis, create_user, create_channel
    ):
        user: User = await create_user()
        channel: Channel = await create_channel()
        await service.earn_token(redis, str(user.id), settings.FEED_PRICE_MAX)

        resp = await client.post(
            settings.API_PATH + "/posts",
            headers=get_jwt_header(user),
            data={
                "channel_id": channel.id,
                "blocks": _blocks_json(_text_block("bomb"), _media_block(0)),
            },
            files=[("files", ("bomb.png", _make_decompression_bomb_png(), "image/png"))],
        )
        assert resp.status_code == 400, resp.text
        assert resp.json()["detail"]["error"] == "post_media_invalid_type"

    async def test_block_order_preserved_including_interleaved_text(
        self, client: AsyncClient, redis: Redis, create_user, create_channel
    ):
        """The point of blocks: text and media in whatever order the author
        arranged them, not text-then-media-strip."""
        user: User = await create_user()
        channel: Channel = await create_channel()
        await service.earn_token(redis, str(user.id), settings.FEED_PRICE_MAX)
        files = [
            ("files", (f"p{i}.jpg", _make_test_jpeg(), "image/jpeg")) for i in range(2)
        ]

        resp = await client.post(
            settings.API_PATH + "/posts",
            headers=get_jwt_header(user),
            data={
                "channel_id": channel.id,
                "blocks": _blocks_json(
                    _text_block("intro"),
                    _media_block(0),
                    _text_block("caption between the two photos"),
                    _media_block(1),
                ),
            },
            files=files,
        )
        assert resp.status_code == 201, resp.text
        blocks = resp.json()["post"]["blocks"]
        assert [b["type"] for b in blocks] == ["text", "media", "text", "media"]
        assert blocks[0]["text"] == "intro"
        assert blocks[2]["text"] == "caption between the two photos"


class TestCreatePostBlocks:
    async def test_zero_blocks_rejected(
        self, client: AsyncClient, redis: Redis, create_user, create_channel
    ):
        user: User = await create_user()
        channel: Channel = await create_channel()
        await service.earn_token(redis, str(user.id), settings.FEED_PRICE_MAX)

        resp = await client.post(
            settings.API_PATH + "/posts",
            headers=get_jwt_header(user),
            data={"channel_id": channel.id, "blocks": _blocks_json()},
        )
        assert resp.status_code == 400, resp.text
        assert resp.json()["detail"]["error"] == "post_blocks_empty"

    async def test_too_many_blocks_rejected(
        self, client: AsyncClient, redis: Redis, create_user, create_channel, monkeypatch
    ):
        monkeypatch.setattr(settings, "POST_BLOCKS_MAX_COUNT", 3)
        user: User = await create_user()
        channel: Channel = await create_channel()
        await service.earn_token(redis, str(user.id), settings.FEED_PRICE_MAX)

        resp = await client.post(
            settings.API_PATH + "/posts",
            headers=get_jwt_header(user),
            data={
                "channel_id": channel.id,
                "blocks": _blocks_json(*[_text_block(f"p{i}") for i in range(4)]),
            },
        )
        assert resp.status_code == 400, resp.text
        assert resp.json()["detail"]["error"] == "post_blocks_too_many"

    async def test_file_index_out_of_range_rejected(
        self, client: AsyncClient, redis: Redis, create_user, create_channel
    ):
        user: User = await create_user()
        channel: Channel = await create_channel()
        await service.earn_token(redis, str(user.id), settings.FEED_PRICE_MAX)

        resp = await client.post(
            settings.API_PATH + "/posts",
            headers=get_jwt_header(user),
            data={
                "channel_id": channel.id,
                "blocks": _blocks_json(_text_block("hi"), _media_block(0)),
            },
            # No files attached at all - file_index 0 is out of range.
        )
        assert resp.status_code == 400, resp.text
        assert resp.json()["detail"]["error"] == "post_blocks_invalid"

    async def test_duplicate_file_index_rejected(
        self, client: AsyncClient, redis: Redis, create_user, create_channel
    ):
        user: User = await create_user()
        channel: Channel = await create_channel()
        await service.earn_token(redis, str(user.id), settings.FEED_PRICE_MAX)

        resp = await client.post(
            settings.API_PATH + "/posts",
            headers=get_jwt_header(user),
            data={
                "channel_id": channel.id,
                "blocks": _blocks_json(_media_block(0), _media_block(0)),
            },
            files=[("files", ("a.jpg", _make_test_jpeg(), "image/jpeg"))],
        )
        assert resp.status_code == 400, resp.text
        assert resp.json()["detail"]["error"] == "post_blocks_invalid"

    async def test_unreferenced_uploaded_file_rejected(
        self, client: AsyncClient, redis: Redis, create_user, create_channel
    ):
        """A file attached but not pointed at by any block is rejected, not
        silently dropped."""
        user: User = await create_user()
        channel: Channel = await create_channel()
        await service.earn_token(redis, str(user.id), settings.FEED_PRICE_MAX)

        resp = await client.post(
            settings.API_PATH + "/posts",
            headers=get_jwt_header(user),
            data={"channel_id": channel.id, "blocks": _blocks_json(_text_block("hi"))},
            files=[("files", ("a.jpg", _make_test_jpeg(), "image/jpeg"))],
        )
        assert resp.status_code == 400, resp.text
        assert resp.json()["detail"]["error"] == "post_blocks_invalid"

    async def test_all_media_post_with_no_text_blocks_succeeds(
        self, client: AsyncClient, redis: Redis, create_user, create_channel
    ):
        user: User = await create_user()
        channel: Channel = await create_channel()
        await service.earn_token(redis, str(user.id), settings.FEED_PRICE_MAX)

        resp = await client.post(
            settings.API_PATH + "/posts",
            headers=get_jwt_header(user),
            data={"channel_id": channel.id, "blocks": _blocks_json(_media_block(0))},
            files=[("files", ("a.jpg", _make_test_jpeg(), "image/jpeg"))],
        )
        assert resp.status_code == 201, resp.text
        assert [b["type"] for b in resp.json()["post"]["blocks"]] == ["media"]

    async def test_all_text_multi_paragraph_post_succeeds(
        self, client: AsyncClient, redis: Redis, create_user, create_channel
    ):
        user: User = await create_user()
        channel: Channel = await create_channel()
        await service.earn_token(redis, str(user.id), settings.FEED_PRICE_MAX)

        resp = await client.post(
            settings.API_PATH + "/posts",
            headers=get_jwt_header(user),
            data={
                "channel_id": channel.id,
                "blocks": _blocks_json(
                    _text_block("first paragraph"), _text_block("second paragraph")
                ),
            },
        )
        assert resp.status_code == 201, resp.text
        blocks = resp.json()["post"]["blocks"]
        assert [b["text"] for b in blocks] == ["first paragraph", "second paragraph"]

    async def test_empty_text_block_rejected(
        self, client: AsyncClient, redis: Redis, create_user, create_channel
    ):
        user: User = await create_user()
        channel: Channel = await create_channel()
        await service.earn_token(redis, str(user.id), settings.FEED_PRICE_MAX)

        resp = await client.post(
            settings.API_PATH + "/posts",
            headers=get_jwt_header(user),
            data={"channel_id": channel.id, "blocks": _blocks_json(_text_block("   "))},
        )
        assert resp.status_code == 400, resp.text
        assert resp.json()["detail"]["error"] == "post_blocks_invalid"

    async def test_malformed_blocks_json_rejected(
        self, client: AsyncClient, redis: Redis, create_user, create_channel
    ):
        user: User = await create_user()
        channel: Channel = await create_channel()
        await service.earn_token(redis, str(user.id), settings.FEED_PRICE_MAX)

        resp = await client.post(
            settings.API_PATH + "/posts",
            headers=get_jwt_header(user),
            data={"channel_id": channel.id, "blocks": "not json"},
        )
        assert resp.status_code == 400, resp.text
        assert resp.json()["detail"]["error"] == "post_blocks_invalid"

    async def test_rejected_block_spends_no_tokens_and_creates_no_post(
        self, client: AsyncClient, db: AsyncSession, redis: Redis, create_user, create_channel
    ):
        user: User = await create_user()
        channel: Channel = await create_channel()
        await service.earn_token(redis, str(user.id), settings.FEED_PRICE_MAX)
        balance_before = await service.token_balance(redis, str(user.id))
        posts_before = await db.scalar(select(func.count()).select_from(Post))

        resp = await client.post(
            settings.API_PATH + "/posts",
            headers=get_jwt_header(user),
            data={
                "channel_id": channel.id,
                "blocks": _blocks_json(_media_block(0), _media_block(0)),
            },
            files=[("files", ("a.jpg", _make_test_jpeg(), "image/jpeg"))],
        )
        assert resp.status_code == 400, resp.text
        assert await service.token_balance(redis, str(user.id)) == balance_before
        assert await db.scalar(select(func.count()).select_from(Post)) == posts_before


class TestPostEconomy:
    async def test_economy_returns_balance_and_price(
        self, client: AsyncClient, redis: Redis, create_user
    ):
        user: User = await create_user()
        await service.earn_token(redis, str(user.id), 3)

        resp = await client.get(
            settings.API_PATH + "/posts/economy", headers=get_jwt_header(user)
        )
        assert resp.status_code == 200, resp.text
        data = resp.json()
        assert data["token_balance"] == 3
        assert data["post_price"] >= settings.FEED_PRICE_MIN
        assert "post_price_expires_at" in data

    async def test_economy_price_is_shared_until_expiry(
        self, client: AsyncClient, redis: Redis, create_user
    ):
        """Two reads must agree, even if congestion changes in between — the whole
        point of the shared snapshot is that clients never see the price drift
        between calls a few seconds apart."""
        user: User = await create_user()

        resp1 = await client.get(
            settings.API_PATH + "/posts/economy", headers=get_jwt_header(user)
        )
        data1 = resp1.json()

        for i in range(settings.FEED_PRICE_TARGET_MIN_ITEMS * 3):
            await service.enqueue_operation(redis, post_id=i, channel_id=1)

        resp2 = await client.get(
            settings.API_PATH + "/posts/economy", headers=get_jwt_header(user)
        )
        data2 = resp2.json()

        assert data2["post_price"] == data1["post_price"]
        assert data2["post_price_expires_at"] == data1["post_price_expires_at"]


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

    async def test_get_post_404_for_subscriber_post_never_in_their_feed(
        self, client: AsyncClient, db: AsyncSession, create_user, create_channel, create_post
    ):
        """Being subscribed to the channel is not enough — the post must actually
        have been delivered to this user's feed (or already reviewed by them)."""
        user: User = await create_user()
        channel: Channel = await create_channel()
        await subscribe(db, user, channel)
        post: Post = await create_post(channel=channel)

        resp = await client.get(
            settings.API_PATH + f"/posts/{post.id}", headers=get_jwt_header(user)
        )
        assert resp.status_code == 404

    async def test_get_post_visible_when_in_users_queue(
        self,
        client: AsyncClient,
        redis: Redis,
        create_user,
        create_channel,
        create_post,
    ):
        user: User = await create_user()
        channel: Channel = await create_channel()
        post: Post = await create_post(channel=channel)
        await service.place_post(redis, str(user.id), post.id)

        resp = await client.get(
            settings.API_PATH + f"/posts/{post.id}", headers=get_jwt_header(user)
        )
        assert resp.status_code == 200, resp.text

    async def test_get_post_visible_after_being_reviewed(
        self,
        client: AsyncClient,
        db: AsyncSession,
        redis: Redis,
        create_user,
        create_channel,
        create_post,
    ):
        user: User = await create_user()
        channel: Channel = await create_channel()
        post: Post = await create_post(channel=channel)
        await service.place_post(redis, str(user.id), post.id)
        await review(db, user, post, "drop")

        resp = await client.get(
            settings.API_PATH + f"/posts/{post.id}", headers=get_jwt_header(user)
        )
        assert resp.status_code == 200, resp.text


class TestPostMediaUrls:
    """Access control for post media, which now lives in the bucket rather than in
    Postgres (see app/core/storage.py).

    The gate did not move, but *when* it runs did: instead of checking
    `_can_view_post` on every byte fetch, the backend checks it once and only then
    mints a presigned URL. So what these assert is that a viewer who may not see
    the post is never handed a URL at all, and that a URL that was handed out
    really does resolve to the bytes - without the bearer token, since the client
    fetches it straight from the bucket.
    """

    _MEDIA_DATA = b"fake-jpeg-bytes-0123456789"
    _MEDIA_KWARGS = {
        "media_type": "image",
        "content_type": "image/jpeg",
        "data": _MEDIA_DATA,
        "size_bytes": len(_MEDIA_DATA),
        "duration_seconds": None,
    }

    @staticmethod
    def _media_url(post_json: dict) -> str:
        return _media_blocks(post_json)[0]["url"]

    async def test_no_url_for_non_subscriber(
        self, client: AsyncClient, create_user, create_post
    ):
        user: User = await create_user()
        post: Post = await create_post(media=[self._MEDIA_KWARGS])
        resp = await client.get(
            settings.API_PATH + f"/posts/{post.id}", headers=get_jwt_header(user)
        )
        # The post itself is invisible, so there is no response to carry a URL.
        assert resp.status_code == 404

    async def test_url_serves_bytes_without_a_bearer_token(
        self,
        client: AsyncClient,
        media_client: AsyncClient,
        redis: Redis,
        create_user,
        create_channel,
        create_post,
    ):
        user: User = await create_user()
        channel: Channel = await create_channel()
        post: Post = await create_post(channel=channel, media=[self._MEDIA_KWARGS])
        await service.place_post(redis, str(user.id), post.id)

        resp = await client.get(
            settings.API_PATH + f"/posts/{post.id}", headers=get_jwt_header(user)
        )
        assert resp.status_code == 200, resp.text
        url = self._media_url(resp.json())

        # Absolute, pointing at the bucket - not a path back into this API.
        assert url.startswith(str(settings.STORAGE_PUBLIC_ENDPOINT_URL))
        fetched = await media_client.get(url)
        assert fetched.status_code == 200, fetched.text
        assert fetched.content == self._MEDIA_DATA
        assert fetched.headers["content-type"] == "image/jpeg"

    async def test_url_visible_to_author_even_if_anonymous(
        self,
        client: AsyncClient,
        media_client: AsyncClient,
        db: AsyncSession,
        create_user,
        create_channel,
        create_post,
    ):
        author: User = await create_user()
        channel: Channel = await create_channel()
        await subscribe(db, author, channel)
        post: Post = await create_post(
            channel=channel, author=author, is_anonymous=True, media=[self._MEDIA_KWARGS]
        )

        resp = await client.get(
            settings.API_PATH + f"/posts/{post.id}", headers=get_jwt_header(author)
        )
        assert resp.status_code == 200, resp.text
        fetched = await media_client.get(self._media_url(resp.json()))
        assert fetched.status_code == 200, fetched.text
        assert fetched.content == self._MEDIA_DATA

    async def test_object_key_does_not_leak_the_author(
        self, db: AsyncSession, create_user, create_channel, create_post
    ):
        """An anonymous post's media URL is shown to every recipient, so a key
        derived from the author would undo the anonymity the post is asking for."""
        author: User = await create_user()
        channel: Channel = await create_channel()
        post: Post = await create_post(
            channel=channel, author=author, is_anonymous=True, media=[self._MEDIA_KWARGS]
        )
        key = post.media[0].object_key
        assert str(author.id) not in key
        assert str(post.id) not in key.rsplit("/", 1)[-1].split(".")[0]

    async def test_tampered_signature_is_rejected(
        self,
        client: AsyncClient,
        media_client: AsyncClient,
        redis: Redis,
        create_user,
        create_channel,
        create_post,
    ):
        """The signature, not obscurity, is what protects the object - a guessed or
        edited URL gets nothing."""
        user: User = await create_user()
        channel: Channel = await create_channel()
        post: Post = await create_post(channel=channel, media=[self._MEDIA_KWARGS])
        await service.place_post(redis, str(user.id), post.id)

        resp = await client.get(
            settings.API_PATH + f"/posts/{post.id}", headers=get_jwt_header(user)
        )
        url = self._media_url(resp.json())

        assert (await media_client.get(url.split("?")[0])).status_code == 403
        tampered = url[:-1] + ("0" if url[-1] != "0" else "1")
        assert (await media_client.get(tampered)).status_code == 403

    async def test_url_is_stable_between_requests(
        self, client: AsyncClient, redis: Redis, create_user, create_channel, create_post
    ):
        """Signatures are quantized to MEDIA_URL_REFRESH_SECONDS (see
        app/core/storage.py), so the same object yields the same string. The client
        caches media by URL, so a per-request signature would silently re-download
        every image in the feed on every refresh."""
        user: User = await create_user()
        channel: Channel = await create_channel()
        post: Post = await create_post(channel=channel, media=[self._MEDIA_KWARGS])
        await service.place_post(redis, str(user.id), post.id)

        headers = get_jwt_header(user)
        first = await client.get(settings.API_PATH + f"/posts/{post.id}", headers=headers)
        second = await client.get(settings.API_PATH + f"/posts/{post.id}", headers=headers)
        assert self._media_url(first.json()) == self._media_url(second.json())

    async def test_bucket_serves_range_requests(
        self,
        client: AsyncClient,
        media_client: AsyncClient,
        redis: Redis,
        create_user,
        create_channel,
        create_post,
    ):
        """Range support is what lets video_player's native ExoPlayer/AVPlayer scrub
        at all. It used to be hand-rolled over an in-memory buffer
        (app/core/http_range.py, now deleted); the bucket does it properly."""
        user: User = await create_user()
        channel: Channel = await create_channel()
        post: Post = await create_post(channel=channel, media=[self._MEDIA_KWARGS])
        await service.place_post(redis, str(user.id), post.id)

        resp = await client.get(
            settings.API_PATH + f"/posts/{post.id}", headers=get_jwt_header(user)
        )
        fetched = await media_client.get(
            self._media_url(resp.json()), headers={"Range": "bytes=0-4"}
        )
        assert fetched.status_code == 206, fetched.text
        assert fetched.content == self._MEDIA_DATA[:5]
        assert (
            fetched.headers["content-range"]
            == f"bytes 0-4/{len(self._MEDIA_DATA)}"
        )


class TestReviewPost:
    async def test_forward_earns_token_reinjects_and_pops(
        self, client: AsyncClient, db: AsyncSession, redis: Redis,
        create_user, create_channel, create_post,
    ):
        user: User = await create_user()
        channel: Channel = await create_channel()
        post: Post = await create_post(channel=channel)
        await service.place_post(redis, str(user.id), post.id)

        resp = await client.post(
            settings.API_PATH + f"/posts/{post.id}/review",
            headers=get_jwt_header(user),
            json={"kind": "forward"},
        )
        assert resp.status_code == 200, resp.text
        data = resp.json()
        assert data["reviewed_count"] == 1
        assert data["token_balance"] == 1  # earned one for reviewing
        # The post's own score, disclosed only here and counting this review.
        assert data["post_forwarded_count"] == 1
        assert data["post_reviewed_count"] == 1

        await db.refresh(post)
        await db.refresh(user)
        assert post.forwarded_count == 1
        assert user.forwarded_count == 1
        # Popped from the queue, and re-injected as an operation (propagation).
        assert await service.render_queue_ids(redis, str(user.id), 10) == []
        assert await service.operation_queue_len(redis) == 1

    async def test_drop_earns_token_without_reinject(
        self, client: AsyncClient, db: AsyncSession, redis: Redis,
        create_user, create_channel, create_post,
    ):
        user: User = await create_user()
        channel: Channel = await create_channel()
        post: Post = await create_post(channel=channel)
        await service.place_post(redis, str(user.id), post.id)

        resp = await client.post(
            settings.API_PATH + f"/posts/{post.id}/review",
            headers=get_jwt_header(user),
            json={"kind": "drop"},
        )
        assert resp.status_code == 200, resp.text
        data = resp.json()
        assert data["token_balance"] == 1
        # A drop counts towards the post's reviewed total but not its forwards.
        assert data["post_forwarded_count"] == 0
        assert data["post_reviewed_count"] == 1
        await db.refresh(post)
        assert post.dropped_count == 1
        assert await service.operation_queue_len(redis) == 0  # no re-injection

    async def test_review_not_in_queue_is_conflict(
        self, client: AsyncClient, redis: Redis, create_user, create_channel, create_post
    ):
        user: User = await create_user()
        channel: Channel = await create_channel()
        post: Post = await create_post(channel=channel)  # never placed in queue

        resp = await client.post(
            settings.API_PATH + f"/posts/{post.id}/review",
            headers=get_jwt_header(user),
            json={"kind": "forward"},
        )
        assert resp.status_code == 409, resp.text
        assert resp.json()["detail"]["error"] == "not_in_queue"

    async def test_review_twice_second_is_conflict(
        self, client: AsyncClient, redis: Redis, create_user, create_channel, create_post
    ):
        user: User = await create_user()
        channel: Channel = await create_channel()
        post: Post = await create_post(channel=channel)
        await service.place_post(redis, str(user.id), post.id)
        header = get_jwt_header(user)

        first = await client.post(
            settings.API_PATH + f"/posts/{post.id}/review",
            headers=header,
            json={"kind": "forward"},
        )
        assert first.status_code == 200, first.text

        second = await client.post(
            settings.API_PATH + f"/posts/{post.id}/review",
            headers=header,
            json={"kind": "drop"},
        )
        assert second.status_code == 409, second.text
        assert second.json()["detail"]["error"] == "not_in_queue"

    async def test_review_nonexistent_post(self, client: AsyncClient, create_user):
        user: User = await create_user()
        resp = await client.post(
            settings.API_PATH + f"/posts/{10**6}/review",
            headers=get_jwt_header(user),
            json={"kind": "forward"},
        )
        assert resp.status_code == 404

    async def test_redelivery_after_review_is_already_reviewed_conflict(
        self,
        client: AsyncClient,
        db: AsyncSession,
        redis: Redis,
        create_user,
        create_channel,
        create_post,
        monkeypatch,
    ):
        """The `post_reviews` unique constraint backstop (see CLAUDE.md /
        app.feed.service `FEED_EXCLUDE_SEEN`): normally `place_post` refuses to
        re-deliver a post the user's `seen` set already has, so this can only be
        reached if that guard is bypassed (e.g. an expired/lost seen set). Simulate
        that by disabling FEED_EXCLUDE_SEEN just for the re-delivery."""
        user: User = await create_user()
        channel: Channel = await create_channel()
        post: Post = await create_post(channel=channel)
        await service.place_post(redis, str(user.id), post.id)
        header = get_jwt_header(user)

        first = await client.post(
            settings.API_PATH + f"/posts/{post.id}/review",
            headers=header,
            json={"kind": "forward"},
        )
        assert first.status_code == 200, first.text

        monkeypatch.setattr(settings, "FEED_EXCLUDE_SEEN", False)
        assert await service.place_post(redis, str(user.id), post.id) == 1
        monkeypatch.undo()

        second = await client.post(
            settings.API_PATH + f"/posts/{post.id}/review",
            headers=header,
            json={"kind": "drop"},
        )
        assert second.status_code == 409, second.text
        assert second.json()["detail"]["error"] == "already_reviewed"
        # Still removed from the queue by the failed attempt's claim.
        assert await service.render_queue_ids(redis, str(user.id), 10) == []
        # The score counts *reviewers*, not encounters: the refused second review
        # moved neither counter. The increment shares the rejected transaction, so
        # the rollback is what enforces this - it is not a separate check.
        await db.refresh(post)
        assert post.forwarded_count == 1
        assert post.dropped_count == 0

    async def test_score_counts_other_reviewers_and_is_never_read_before(
        self, client: AsyncClient, db: AsyncSession, redis: Redis,
        create_user, create_channel, create_post,
    ):
        """The score a reviewer is shown reflects everyone who reviewed before them -
        and none of the read routes hand it over beforehand, which is the whole
        point: a reader must not be able to check the crowd through the raw API."""
        author: User = await create_user()
        channel: Channel = await create_channel()
        post: Post = await create_post(channel=channel, author=author)
        early: User = await create_user()
        await review(db, early, post, "forward")
        other: User = await create_user()
        await review(db, other, post, "drop")

        user: User = await create_user()
        await service.place_post(redis, str(user.id), post.id)

        # Both routes that can render this post to its reviewer, before the verdict.
        queued = await client.get(
            settings.API_PATH + "/posts/feed", headers=get_jwt_header(user)
        )
        assert queued.status_code == 200, queued.text
        detail = await client.get(
            settings.API_PATH + f"/posts/{post.id}", headers=get_jwt_header(user)
        )
        assert detail.status_code == 200, detail.text
        for body in (_feed_posts(queued.json())[0], detail.json()):
            assert "forwarded_count" not in body
            assert "dropped_count" not in body

        resp = await client.post(
            settings.API_PATH + f"/posts/{post.id}/review",
            headers=get_jwt_header(user),
            json={"kind": "forward"},
        )
        assert resp.status_code == 200, resp.text
        data = resp.json()
        assert data["post_forwarded_count"] == 2  # the early forward plus this one
        assert data["post_reviewed_count"] == 3  # ...and the drop in between

        # Nor afterwards, on the author's own list or the reviewed history.
        mine = await client.get(
            settings.API_PATH + "/posts/mine", headers=get_jwt_header(author)
        )
        assert mine.status_code == 200, mine.text
        assert "forwarded_count" not in mine.json()[0]
        history = await client.get(
            settings.API_PATH + "/posts/reviewed", headers=get_jwt_header(user)
        )
        assert history.status_code == 200, history.text
        assert "forwarded_count" not in history.json()[0]["post"]


class TestDeliveryExclusions:
    async def test_author_never_receives_their_own_published_post(
        self, client: AsyncClient, db: AsyncSession, redis: Redis,
        create_user, create_channel,
    ):
        """End to end: publish, then fan out the way the background consumer would.
        The author is the channel's only subscriber, so nobody gets it — and crucially
        the author's own feed stays empty."""
        author: User = await create_user()
        channel: Channel = await create_channel()
        await subscribe(db, author, channel)
        await service.sync_subscribe(redis, str(author.id), channel.id)
        await service.earn_token(redis, str(author.id), settings.FEED_PRICE_MAX)
        header = get_jwt_header(author)

        resp = await client.post(
            settings.API_PATH + "/posts",
            headers=header,
            data={"channel_id": channel.id, "blocks": _blocks_json(_text_block("mine"))},
        )
        assert resp.status_code == 201, resp.text

        # The author rides along on the operation — that is the whole mechanism.
        entries = await redis.xrange(keys.STREAM)
        assert entries[0][1]["author_id"] == str(author.id)

        await consume_once(redis, "test-consumer", timeout=2.0)

        feed = await client.get(settings.API_PATH + "/posts/feed", headers=header)
        assert feed.status_code == 200, feed.text
        assert feed.json() == []

    async def test_forwarded_post_does_not_return_to_the_forwarder(
        self, client: AsyncClient, db: AsyncSession, redis: Redis,
        create_user, create_channel, create_post,
    ):
        """The loop that used to bite: forwarding re-injects the post, and before the
        seen set the forwarder was a perfectly valid recipient for it again."""
        user: User = await create_user()
        channel: Channel = await create_channel()
        post: Post = await create_post(channel=channel)
        await subscribe(db, user, channel)
        await service.sync_subscribe(redis, str(user.id), channel.id)
        await service.place_post(redis, str(user.id), post.id)
        header = get_jwt_header(user)

        resp = await client.post(
            settings.API_PATH + f"/posts/{post.id}/review",
            headers=header,
            json={"kind": "forward"},
        )
        assert resp.status_code == 200, resp.text

        await consume_once(redis, "test-consumer", timeout=2.0)

        feed = await client.get(settings.API_PATH + "/posts/feed", headers=header)
        assert feed.json() == []


class TestInteractionRateLimit:
    """The per-user budget shared by create/forward/drop (app/deps/rate_limit.py)."""

    async def test_review_burst_past_the_limit_is_throttled(
        self, client: AsyncClient, redis: Redis, create_user, create_channel, create_post
    ):
        user: User = await create_user()
        channel: Channel = await create_channel()
        limit = settings.INTERACTION_RATE_LIMIT
        posts = [await create_post(channel=channel) for _ in range(limit + 1)]
        for post in posts:
            await service.place_post(redis, str(user.id), post.id)
        header = get_jwt_header(user)

        for post in posts[:limit]:
            resp = await client.post(
                settings.API_PATH + f"/posts/{post.id}/review",
                headers=header,
                json={"kind": "drop"},
            )
            assert resp.status_code == 200, resp.text

        resp = await client.post(
            settings.API_PATH + f"/posts/{posts[-1].id}/review",
            headers=header,
            json={"kind": "drop"},
        )
        assert resp.status_code == 429, resp.text
        body = resp.json()["detail"]
        assert body["error"] == "rate_limited"
        assert 1 <= body["retry_after"] <= settings.INTERACTION_RATE_WINDOW_SECONDS
        assert resp.headers["Retry-After"] == str(body["retry_after"])
        # Rejected before the handler ran: the post stays reviewable.
        assert await service.render_queue_ids(redis, str(user.id), 10) == [posts[-1].id]

    async def test_budget_is_shared_between_reviewing_and_posting(
        self, client: AsyncClient, redis: Redis, create_user, create_channel, create_post
    ):
        """Alternating between endpoints must not buy extra interactions."""
        user: User = await create_user()
        channel: Channel = await create_channel()
        limit = settings.INTERACTION_RATE_LIMIT
        posts = [await create_post(channel=channel) for _ in range(limit)]
        for post in posts:
            await service.place_post(redis, str(user.id), post.id)
        await service.earn_token(redis, str(user.id), settings.FEED_PRICE_MAX)
        header = get_jwt_header(user)

        for post in posts:
            resp = await client.post(
                settings.API_PATH + f"/posts/{post.id}/review",
                headers=header,
                json={"kind": "drop"},
            )
            assert resp.status_code == 200, resp.text

        balance_before = await service.token_balance(redis, str(user.id))
        resp = await client.post(
            settings.API_PATH + "/posts",
            headers=header,
            data={
                "channel_id": channel.id,
                "blocks": _blocks_json(_text_block("one too many")),
            },
        )
        assert resp.status_code == 429, resp.text
        # Throttled ahead of the handler, so nothing was charged for it.
        assert await service.token_balance(redis, str(user.id)) == balance_before

    async def test_superuser_is_exempt(
        self, client: AsyncClient, db: AsyncSession, redis: Redis,
        create_user, create_channel, create_post,
    ):
        user: User = await create_user()
        user.is_superuser = True
        db.add(user)
        await db.commit()
        channel: Channel = await create_channel()
        posts = [
            await create_post(channel=channel)
            for _ in range(settings.INTERACTION_RATE_LIMIT + 1)
        ]
        for post in posts:
            await service.place_post(redis, str(user.id), post.id)
        header = get_jwt_header(user)

        for post in posts:
            resp = await client.post(
                settings.API_PATH + f"/posts/{post.id}/review",
                headers=header,
                json={"kind": "drop"},
            )
            assert resp.status_code == 200, resp.text


class TestMyPosts:
    async def test_only_returns_own_posts_newest_first(
        self,
        client: AsyncClient,
        db: AsyncSession,
        create_user,
        create_channel,
        create_post,
    ):

        user: User = await create_user()
        other: User = await create_user()
        channel: Channel = await create_channel()
        now = datetime.now(timezone.utc)

        older: Post = await create_post(channel=channel, author=user, text="older")
        older.created = now - timedelta(days=1)
        newer: Post = await create_post(channel=channel, author=user, text="newer")
        newer.created = now
        db.add_all([older, newer])
        await db.commit()

        await create_post(channel=channel, author=other, text="not mine")

        resp = await client.get(
            settings.API_PATH + "/posts/mine", headers=get_jwt_header(user)
        )
        assert resp.status_code == 200, resp.text
        data = resp.json()
        assert [p["id"] for p in data] == [newer.id, older.id]

    async def test_pagination_via_skip_limit(
        self, client: AsyncClient, create_user, create_channel, create_post
    ):
        user: User = await create_user()
        channel: Channel = await create_channel()
        for _ in range(3):
            await create_post(channel=channel, author=user)

        resp = await client.get(
            settings.API_PATH + "/posts/mine",
            headers=get_jwt_header(user),
            params={"skip": 1, "limit": 1},
        )
        assert resp.status_code == 200, resp.text
        assert len(resp.json()) == 1


class TestMyReviewedPosts:
    async def test_sorted_by_review_time_not_post_creation_time(
        self,
        client: AsyncClient,
        db: AsyncSession,
        create_user,
        create_channel,
        create_post,
    ):

        user: User = await create_user()
        channel: Channel = await create_channel()
        now = datetime.now(timezone.utc)

        # post_a was created first but reviewed last; post_b is the opposite.
        # A creation-time sort would put post_a first — a review-time sort must not.
        post_a: Post = await create_post(channel=channel, text="a")
        post_a.created = now - timedelta(days=2)
        post_b: Post = await create_post(channel=channel, text="b")
        post_b.created = now - timedelta(days=1)
        db.add_all([post_a, post_b])
        await db.commit()

        review_a = await review(db, user, post_a, "drop")
        review_a.created = now
        review_b = await review(db, user, post_b, "forward")
        review_b.created = now - timedelta(hours=1)
        db.add_all([review_a, review_b])
        await db.commit()

        resp = await client.get(
            settings.API_PATH + "/posts/reviewed", headers=get_jwt_header(user)
        )
        assert resp.status_code == 200, resp.text
        data = resp.json()
        assert [d["post"]["id"] for d in data] == [post_a.id, post_b.id]
        assert data[0]["kind"] == "drop"
        assert data[1]["kind"] == "forward"

    async def test_only_returns_own_reviews(
        self, client: AsyncClient, db: AsyncSession, create_user, create_channel, create_post
    ):
        user: User = await create_user()
        other: User = await create_user()
        channel: Channel = await create_channel()
        post: Post = await create_post(channel=channel)

        await review(db, other, post, "forward")

        resp = await client.get(
            settings.API_PATH + "/posts/reviewed", headers=get_jwt_header(user)
        )
        assert resp.status_code == 200, resp.text
        assert resp.json() == []
