"""The API surface of language routing: declaring a post's language, and choosing
which languages you accept."""

import uuid

from httpx import AsyncClient
from redis.asyncio import Redis
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.core.languages import UNSPECIFIED
from app.feed import keys, service
from app.models.channel import Channel
from app.models.user import User
from tests.utils import get_jwt_header, subscribe

_TEXT = '[{"type": "text", "text": "guten tag"}]'


class TestPostLanguage:
    async def test_declared_language_is_stored_and_returned(
        self, client: AsyncClient, redis: Redis, create_user, create_channel
    ):
        user: User = await create_user()
        channel: Channel = await create_channel()
        await service.earn_token(redis, str(user.id), settings.FEED_PRICE_MAX)

        resp = await client.post(
            settings.API_PATH + "/posts",
            headers=get_jwt_header(user),
            data={"channel_id": channel.id, "language": "de", "blocks": _TEXT},
        )

        assert resp.status_code == 201, resp.text
        assert resp.json()["post"]["language"] == "de"

    async def test_unknown_language_is_rejected(
        self, client: AsyncClient, redis: Redis, create_user, create_channel
    ):
        user: User = await create_user()
        channel: Channel = await create_channel()
        await service.earn_token(redis, str(user.id), settings.FEED_PRICE_MAX)

        resp = await client.post(
            settings.API_PATH + "/posts",
            headers=get_jwt_header(user),
            data={"channel_id": channel.id, "language": "kl", "blocks": _TEXT},
        )

        assert resp.status_code == 400, resp.text
        assert resp.json()["detail"]["error"] == "post_language_invalid"

    async def test_unspecified_is_refused_for_a_post_containing_text(
        self, client: AsyncClient, redis: Redis, create_user, create_channel
    ):
        """"No language" routes through the whole channel, so it is the widest
        audience a post can claim - and text is the one part of that claim the server
        can check without reading the post."""
        user: User = await create_user()
        channel: Channel = await create_channel()
        await service.earn_token(redis, str(user.id), settings.FEED_PRICE_MAX)

        resp = await client.post(
            settings.API_PATH + "/posts",
            headers=get_jwt_header(user),
            data={
                "channel_id": channel.id,
                "language": UNSPECIFIED,
                "blocks": _TEXT,
            },
        )

        assert resp.status_code == 400, resp.text
        assert resp.json()["detail"]["error"] == "post_language_requires_no_text"

    async def test_a_language_is_rejected_before_any_tokens_are_spent(
        self, client: AsyncClient, redis: Redis, create_user, create_channel
    ):
        user: User = await create_user()
        channel: Channel = await create_channel()
        await service.earn_token(redis, str(user.id), settings.FEED_PRICE_MAX)
        before = await service.token_balance(redis, str(user.id))

        await client.post(
            settings.API_PATH + "/posts",
            headers=get_jwt_header(user),
            data={"channel_id": channel.id, "language": "kl", "blocks": _TEXT},
        )

        assert await service.token_balance(redis, str(user.id)) == before


class TestContentLanguagesRoute:
    async def test_registration_narrows_to_the_request_locale(
        self, client: AsyncClient
    ):
        """The column default is every language; a real signup is narrowed to what the
        request asked for, so a reader is never handed posts they cannot read purely
        because they never visited the setting."""
        email = f"{uuid.uuid4().hex}@example.com"
        resp = await client.post(
            settings.API_PATH + "/auth/register",
            json={
                "email": email,
                "password": "sup3rSecret!",
                "username": uuid.uuid4().hex[:12],
            },
            headers={"Accept-Language": "de"},
        )

        assert resp.status_code == 201, resp.text
        assert resp.json()["content_languages"] == ["de"]

    async def test_setting_languages_rewrites_audience_membership(
        self,
        client: AsyncClient,
        db: AsyncSession,
        redis: Redis,
        create_user,
        create_channel,
    ):
        user: User = await create_user()
        channel: Channel = await create_channel()
        await subscribe(db, user, channel)
        await service.sync_subscribe(redis, str(user.id), channel.id, ["en"])

        resp = await client.put(
            settings.API_PATH + "/users/me/content-languages",
            headers=get_jwt_header(user),
            json={"languages": ["de"]},
        )

        assert resp.status_code == 200, resp.text
        assert resp.json()["content_languages"] == ["de"]
        assert await redis.sismember(keys.audience(channel.id, "de"), str(user.id))
        assert not await redis.sismember(keys.audience(channel.id, "en"), str(user.id))

    async def test_an_empty_set_is_refused(
        self, client: AsyncClient, create_user
    ):
        """An audience of nowhere: no route would ever select this reader, and their
        feed would be permanently empty with nothing to explain it."""
        user: User = await create_user()

        resp = await client.put(
            settings.API_PATH + "/users/me/content-languages",
            headers=get_jwt_header(user),
            json={"languages": []},
        )

        assert resp.status_code == 400, resp.text
        assert resp.json()["detail"]["error"] == "content_languages_empty"

    async def test_an_unknown_language_is_refused_not_filtered(
        self, client: AsyncClient, create_user
    ):
        """Storing the valid subset would leave the client believing a preference the
        server never accepted."""
        user: User = await create_user()

        resp = await client.put(
            settings.API_PATH + "/users/me/content-languages",
            headers=get_jwt_header(user),
            json={"languages": ["en", "kl"]},
        )

        assert resp.status_code == 400, resp.text
        assert resp.json()["detail"]["error"] == "content_languages_invalid"

    async def test_is_canonicalized_and_deduplicated(
        self, client: AsyncClient, create_user
    ):
        user: User = await create_user()

        resp = await client.put(
            settings.API_PATH + "/users/me/content-languages",
            headers=get_jwt_header(user),
            json={"languages": ["de", "en", "de"]},
        )

        assert resp.status_code == 200, resp.text
        # Ordered by CONTENT_LANGUAGES, not by input, so equivalent sets compare equal
        # and a no-op update is recognizable as one.
        assert resp.json()["content_languages"] == ["en", "de"]

    async def test_settings_revision_moves_only_on_a_real_change(
        self, client: AsyncClient, create_user
    ):
        """A client that re-asserts its preference on every launch must not
        manufacture a settings conflict for the user's other devices."""
        user: User = await create_user()
        headers = get_jwt_header(user)

        first = await client.put(
            settings.API_PATH + "/users/me/content-languages",
            headers=headers,
            json={"languages": ["de"]},
        )
        revision = first.json()["settings_revision"]

        second = await client.put(
            settings.API_PATH + "/users/me/content-languages",
            headers=headers,
            json={"languages": ["de"]},
        )

        assert second.json()["settings_revision"] == revision


class TestPublicConfigAdvertisesLanguages:
    async def test_config_serves_the_language_list(self, client: AsyncClient):
        """Served rather than hardcoded client-side, so the picker, the client's
        detector and the values the API accepts cannot drift apart."""
        resp = await client.get(settings.API_PATH + "/config")

        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["content_languages"] == settings.CONTENT_LANGUAGES
        assert body["language_unspecified"] == UNSPECIFIED
