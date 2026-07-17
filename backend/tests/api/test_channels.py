from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.models.channel import Channel
from app.models.user import User
from tests.utils import generate_random_string, get_jwt_header, subscribe


class TestListChannels:
    async def test_list_channels_not_logged_in(self, client: AsyncClient):
        resp = await client.get(settings.API_PATH + "/channels")
        assert resp.status_code == 401

    async def test_list_channels_is_subscribed_flag(
        self, client: AsyncClient, db: AsyncSession, create_user, create_channel
    ):
        user: User = await create_user()
        subscribed: Channel = await create_channel()
        unsubscribed: Channel = await create_channel()
        await subscribe(db, user, subscribed)

        resp = await client.get(
            settings.API_PATH + "/channels", headers=get_jwt_header(user)
        )
        assert resp.status_code == 200, resp.text
        by_id = {c["id"]: c for c in resp.json()}
        assert by_id[subscribed.id]["is_subscribed"] is True
        assert by_id[unsubscribed.id]["is_subscribed"] is False

    async def test_list_channels_search_filter(
        self, client: AsyncClient, create_user, create_channel
    ):
        user: User = await create_user()
        unique_name = f"search-target-{generate_random_string(10)}"
        await create_channel(name=unique_name, description="desc")
        resp = await client.get(
            settings.API_PATH + f"/channels?q={unique_name}",
            headers=get_jwt_header(user),
        )
        assert resp.status_code == 200, resp.text
        names = [c["name"] for c in resp.json()]
        assert unique_name in names


class TestSubscribeChannel:
    async def test_subscribe_is_idempotent(
        self, client: AsyncClient, create_user, create_channel
    ):
        user: User = await create_user()
        channel: Channel = await create_channel()
        jwt_header = get_jwt_header(user)

        for _ in range(2):
            resp = await client.post(
                settings.API_PATH + f"/channels/{channel.id}/subscribe",
                headers=jwt_header,
            )
            assert resp.status_code == 200, resp.text
            assert resp.json()["is_subscribed"] is True

    async def test_subscribe_nonexistent_channel(
        self, client: AsyncClient, create_user
    ):
        user: User = await create_user()
        resp = await client.post(
            settings.API_PATH + f"/channels/{10**6}/subscribe",
            headers=get_jwt_header(user),
        )
        assert resp.status_code == 404


class TestUnsubscribeChannel:
    async def test_unsubscribe_noop_when_not_subscribed(
        self, client: AsyncClient, create_user, create_channel
    ):
        user: User = await create_user()
        channel: Channel = await create_channel()
        resp = await client.post(
            settings.API_PATH + f"/channels/{channel.id}/unsubscribe",
            headers=get_jwt_header(user),
        )
        assert resp.status_code == 200, resp.text
        assert resp.json()["is_subscribed"] is False

    async def test_unsubscribe_after_subscribe(
        self, client: AsyncClient, db: AsyncSession, create_user, create_channel
    ):
        user: User = await create_user()
        channel: Channel = await create_channel()
        await subscribe(db, user, channel)

        resp = await client.post(
            settings.API_PATH + f"/channels/{channel.id}/unsubscribe",
            headers=get_jwt_header(user),
        )
        assert resp.status_code == 200, resp.text
        assert resp.json()["is_subscribed"] is False
