from collections.abc import Callable

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
                "password": "Sup3rSecret!23",
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
                "password": "Sup3rSecret!23",
                "username": generate_random_string(15),
            },
        )
        assert resp.status_code == 201, resp.text
        assert resp.json()["onboarding_completed"] is False

    async def test_duplicate_email_is_a_structured_error(self, client: AsyncClient):
        email = f"{generate_random_string(20)}@{generate_random_string(10)}.com"
        payload = {
            "email": email,
            "password": "Sup3rSecret!23",
            "username": generate_random_string(15),
        }
        resp = await client.post(settings.API_PATH + "/auth/register", json=payload)
        assert resp.status_code == 201, resp.text

        payload["username"] = generate_random_string(15)
        resp = await client.post(settings.API_PATH + "/auth/register", json=payload)
        assert resp.status_code == 400
        assert resp.json()["detail"]["error"] == "register_user_already_exists"

    async def test_weak_password_is_rejected_with_a_reason(self, client: AsyncClient):
        """REQUIRE_STRONG_PASSWORD defaults on - see app.core.password_policy."""
        assert settings.REQUIRE_STRONG_PASSWORD
        email = f"{generate_random_string(20)}@{generate_random_string(10)}.com"
        resp = await client.post(
            settings.API_PATH + "/auth/register",
            json={
                "email": email,
                "password": "weak",
                "username": generate_random_string(15),
            },
        )
        assert resp.status_code == 400
        detail = resp.json()["detail"]
        assert detail["error"] == "register_invalid_password"
        assert detail["reason"]


class TestLogin:
    async def test_wrong_password_is_a_structured_error(
        self, client: AsyncClient, create_user: Callable
    ):
        user = await create_user()
        resp = await client.post(
            settings.API_PATH + "/auth/jwt/login",
            data={"username": user.email, "password": "definitely-not-it"},
        )
        assert resp.status_code == 400
        assert resp.json()["detail"]["error"] == "login_bad_credentials"

    async def test_unknown_email_is_the_same_structured_error(
        self, client: AsyncClient
    ):
        """Deliberately indistinguishable from a wrong password, to avoid leaking
        which emails have accounts."""
        resp = await client.post(
            settings.API_PATH + "/auth/jwt/login",
            data={
                "username": f"{generate_random_string(20)}@nowhere.com",
                "password": "whatever123",
            },
        )
        assert resp.status_code == 400
        assert resp.json()["detail"]["error"] == "login_bad_credentials"

    async def test_unverified_user_can_still_log_in(
        self, client: AsyncClient, create_user: Callable, default_password: str
    ):
        """Login must succeed while unverified - the client needs the token to call
        the verification endpoints at all. See app.deps.users.CurrentVerifiedUser
        for where unverified users actually get blocked."""
        user = await create_user(is_verified=False)
        resp = await client.post(
            settings.API_PATH + "/auth/jwt/login",
            data={"username": user.email, "password": default_password},
        )
        assert resp.status_code == 200, resp.text
        assert "access_token" in resp.json()


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
