from collections.abc import Callable

from httpx import AsyncClient
from redis.asyncio import Redis
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.feed import service
from app.models.user import User
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

    async def test_weak_password_is_rejected_with_a_reason(
        self, client: AsyncClient, monkeypatch
    ):
        """REQUIRE_STRONG_PASSWORD defaults off (see app.core.config) - force it on
        to exercise the rejection path, see app.core.password_policy."""
        monkeypatch.setattr(settings, "REQUIRE_STRONG_PASSWORD", True)
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
        # "weak" is both too short and single-character-class against the
        # default PASSWORD_MIN_LENGTH/PASSWORD_MIN_CHARACTER_CLASSES, so both
        # structured violation codes should be present (see
        # app.core.password_policy.strength_violations).
        codes = {v["code"] for v in detail["reason"]}
        assert codes == {"password_too_short", "password_missing_variety"}

    async def test_weak_password_is_accepted_when_policy_off(
        self, client: AsyncClient
    ):
        """Off is the default - any password, however weak, must be accepted."""
        assert settings.REQUIRE_STRONG_PASSWORD is False
        email = f"{generate_random_string(20)}@{generate_random_string(10)}.com"
        resp = await client.post(
            settings.API_PATH + "/auth/register",
            json={
                "email": email,
                "password": "weak",
                "username": generate_random_string(15),
            },
        )
        assert resp.status_code == 201, resp.text


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


class TestListUsers:
    """GET /users - superuser-only, react-admin-style listing (app/api/users.py)."""

    async def test_not_logged_in(self, client: AsyncClient):
        resp = await client.get(settings.API_PATH + "/users")
        assert resp.status_code == 401

    async def test_non_superuser_rejected(
        self, client: AsyncClient, create_user: Callable
    ):
        user = await create_user()
        resp = await client.get(
            settings.API_PATH + "/users", headers=get_jwt_header(user)
        )
        assert resp.status_code == 403

    async def test_superuser_lists_users_with_content_range(
        self, client: AsyncClient, db: AsyncSession, create_user: Callable
    ):
        superuser: User = await create_user()
        superuser.is_superuser = True
        db.add(superuser)
        await db.commit()

        resp = await client.get(
            settings.API_PATH + "/users",
            # A large limit: many users accumulate across the test session and the
            # endpoint applies no ordering, so the default page might not include
            # the one just created.
            params={"limit": 100_000},
            headers=get_jwt_header(superuser),
        )
        assert resp.status_code == 200, resp.text
        assert "Content-Range" in resp.headers
        ids = {u["id"] for u in resp.json()}
        assert str(superuser.id) in ids

    async def test_superuser_pagination(
        self, client: AsyncClient, db: AsyncSession, create_user: Callable
    ):
        superuser: User = await create_user()
        superuser.is_superuser = True
        db.add(superuser)
        await db.commit()

        resp = await client.get(
            settings.API_PATH + "/users",
            params={"skip": 0, "limit": 1},
            headers=get_jwt_header(superuser),
        )
        assert resp.status_code == 200, resp.text
        assert len(resp.json()) == 1
        start, rest = resp.headers["Content-Range"].split("-", 1)
        end, total = rest.split("/")
        assert start == "0"
        assert end == "1"
        assert int(total) >= 1


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
