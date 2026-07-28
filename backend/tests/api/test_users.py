from httpx import AsyncClient
from redis.asyncio import Redis

from app.core.config import settings
from app.feed import service
from tests.utils import generate_random_string, get_jwt_header


class TestRegister:
    async def test_new_account_starts_with_the_starting_token_grant(
        self, client: AsyncClient, redis: Redis
    ):
        # Registration commits straight to Postgres (fastapi-users' own session,
        # outside the `auto_rollback` fixture's transaction), so a fixed email
        # would collide with a leftover row the moment this test runs twice —
        # random, like `create_user`'s fixture, to actually get a fresh account.
        email = f"{generate_random_string(20)}@{generate_random_string(10)}.com"
        resp = await client.post(
            settings.API_PATH + "/auth/register",
            json={
                "email": email,
                "password": "supersecret123",
                "username": generate_random_string(15),
            },
        )
        assert resp.status_code == 201, resp.text
        user_id = resp.json()["id"]

        balance = await service.token_balance(redis, user_id)
        assert balance == settings.FEED_STARTING_TOKENS

    async def test_new_account_has_not_completed_onboarding(
        self, client: AsyncClient
    ):
        email = f"{generate_random_string(20)}@{generate_random_string(10)}.com"
        resp = await client.post(
            settings.API_PATH + "/auth/register",
            json={
                "email": email,
                "password": "supersecret123",
                "username": generate_random_string(15),
            },
        )
        assert resp.status_code == 201, resp.text
        assert resp.json()["onboarding_completed"] is False


class TestUpdateMe:
    async def test_completing_onboarding_persists(self, client: AsyncClient, create_user):
        user = await create_user()
        resp = await client.patch(
            settings.API_PATH + "/users/me",
            json={"onboarding_completed": True},
            headers=get_jwt_header(user),
        )
        assert resp.status_code == 200, resp.text
        assert resp.json()["onboarding_completed"] is True

        resp = await client.get(
            settings.API_PATH + "/users/me", headers=get_jwt_header(user)
        )
        assert resp.json()["onboarding_completed"] is True
