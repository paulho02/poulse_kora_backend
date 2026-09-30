"""Forgot/reset-password: the short-code flow and its enumeration-safety rules.

See CLAUDE.md / app.core.password_reset for why this is a code stored in Redis
rather than fastapi-users' own link-based forgot/reset-password flow, and
app/api/password_reset.py for why every response is shaped to never disclose
whether a submitted email belongs to an account.
"""

from unittest.mock import AsyncMock

from httpx import AsyncClient
from redis.asyncio import Redis
from sqlalchemy.ext.asyncio import AsyncSession

from app.api import password_reset as password_reset_api
from app.core import password_reset as pr
from app.core.config import settings
from app.models.oauth_account import GOOGLE_OAUTH_NAME, OAuthAccount
from app.models.user import User
from tests.utils import generate_random_string

FORGOT = settings.API_PATH + "/auth/forgot-password"
CONFIRM = settings.API_PATH + "/auth/reset-password/confirm"


def _random_email() -> str:
    return f"{generate_random_string(20)}@{generate_random_string(10)}.com"


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


class TestForgotPassword:
    async def test_existing_account_gets_generic_response_and_a_code(
        self, client: AsyncClient, create_user, redis: Redis, monkeypatch
    ):
        mock_send = AsyncMock()
        monkeypatch.setattr(password_reset_api, "send_email", mock_send)
        user = await create_user()

        resp = await client.post(FORGOT, json={"email": user.email})
        assert resp.status_code == 200, resp.text
        assert resp.json() == {"msg": "ok"}

        mock_send.assert_awaited_once()
        assert mock_send.await_args.args[0] == user.email
        assert await redis.get(f"pwd_reset:code:{user.id}") is not None

    async def test_unknown_email_gets_the_identical_response(
        self, client: AsyncClient, monkeypatch
    ):
        mock_send = AsyncMock()
        monkeypatch.setattr(password_reset_api, "send_email", mock_send)

        resp = await client.post(FORGOT, json={"email": _random_email()})
        assert resp.status_code == 200
        assert resp.json() == {"msg": "ok"}
        mock_send.assert_not_awaited()

    async def test_google_linked_account_gets_a_notice_not_a_code(
        self,
        client: AsyncClient,
        create_user,
        db: AsyncSession,
        redis: Redis,
        monkeypatch,
    ):
        mock_send = AsyncMock()
        monkeypatch.setattr(password_reset_api, "send_email", mock_send)
        user = await create_user()
        await _link_google(db, user)

        resp = await client.post(FORGOT, json={"email": user.email})
        assert resp.status_code == 200
        assert resp.json() == {"msg": "ok"}

        mock_send.assert_awaited_once()
        assert mock_send.await_args.args[0] == user.email
        # No code was ever issued - there is no password to redeem one for.
        assert await redis.get(f"pwd_reset:code:{user.id}") is None

    async def test_inactive_account_gets_the_generic_response_and_no_email(
        self,
        client: AsyncClient,
        create_user,
        db: AsyncSession,
        redis: Redis,
        monkeypatch,
    ):
        mock_send = AsyncMock()
        monkeypatch.setattr(password_reset_api, "send_email", mock_send)
        user = await create_user()
        user.is_active = False
        db.add(user)
        await db.commit()

        resp = await client.post(FORGOT, json={"email": user.email})
        assert resp.status_code == 200
        assert resp.json() == {"msg": "ok"}
        mock_send.assert_not_awaited()
        assert await redis.get(f"pwd_reset:code:{user.id}") is None

    async def test_send_failure_still_answers_generically(
        self, client: AsyncClient, create_user, monkeypatch
    ):
        monkeypatch.setattr(
            password_reset_api,
            "send_email",
            AsyncMock(side_effect=OSError("smtp down")),
        )
        user = await create_user()
        resp = await client.post(FORGOT, json={"email": user.email})
        assert resp.status_code == 200
        assert resp.json() == {"msg": "ok"}


class TestForgotPasswordRateLimitIsEnumerationSafe:
    """The account-keyed budget (app/deps/rate_limit.py: limit_forgot_password)
    must be spent identically whether or not the submitted email belongs to an
    account - otherwise the 429 itself would answer "does this exist?"."""

    async def test_existing_and_unknown_emails_hit_the_same_limit(
        self, client: AsyncClient, create_user, monkeypatch
    ):
        monkeypatch.setattr(
            settings, "PASSWORD_RESET_REQUEST_RATE_LIMIT_PER_ACCOUNT", 2
        )
        monkeypatch.setattr(settings, "PASSWORD_RESET_REQUEST_RATE_LIMIT_PER_IP", 100)
        user = await create_user()

        for email in (user.email, _random_email()):
            for _ in range(2):
                assert (
                    await client.post(FORGOT, json={"email": email})
                ).status_code == 200
            resp = await client.post(FORGOT, json={"email": email})
            assert resp.status_code == 429
            assert resp.json()["detail"]["error"] == "rate_limited"
            assert "Retry-After" in resp.headers


class TestResetPasswordConfirm:
    async def test_correct_code_resets_the_password(
        self, client: AsyncClient, create_user, redis: Redis
    ):
        user = await create_user()
        code = await pr.issue_code(redis, str(user.id))

        resp = await client.post(
            CONFIRM,
            json={
                "email": user.email,
                "code": code,
                "new_password": "brand-new-pw-1",
            },
        )
        assert resp.status_code == 204, resp.text

        resp = await client.post(
            settings.API_PATH + "/auth/jwt/login",
            data={"username": user.email, "password": "brand-new-pw-1"},
        )
        assert resp.status_code == 200, resp.text
        assert "access_token" in resp.json()

    async def test_code_is_single_use(
        self, client: AsyncClient, create_user, redis: Redis
    ):
        user = await create_user()
        code = await pr.issue_code(redis, str(user.id))
        payload = {
            "email": user.email,
            "code": code,
            "new_password": "brand-new-pw-1",
        }
        assert (await client.post(CONFIRM, json=payload)).status_code == 204

        resp = await client.post(
            CONFIRM, json={**payload, "new_password": "another-new-pw-2"}
        )
        assert resp.status_code == 400
        assert (
            resp.json()["detail"]["error"] == "password_reset_invalid_or_expired_code"
        )

    async def test_wrong_code_reports_attempts_remaining(
        self, client: AsyncClient, create_user, redis: Redis
    ):
        user = await create_user()
        await pr.issue_code(redis, str(user.id))

        resp = await client.post(
            CONFIRM,
            json={
                "email": user.email,
                "code": "000000",
                "new_password": "brand-new-pw-1",
            },
        )
        assert resp.status_code == 400
        detail = resp.json()["detail"]
        assert detail["error"] == "password_reset_invalid_or_expired_code"
        assert detail["attempts_remaining"] == settings.PASSWORD_RESET_MAX_ATTEMPTS - 1

    async def test_unknown_email_produces_the_identical_error_as_a_wrong_code(
        self, client: AsyncClient, create_user, redis: Redis
    ):
        user = await create_user()
        await pr.issue_code(redis, str(user.id))

        wrong_code_resp = await client.post(
            CONFIRM,
            json={
                "email": user.email,
                "code": "000000",
                "new_password": "brand-new-pw-1",
            },
        )
        unknown_email_resp = await client.post(
            CONFIRM,
            json={
                "email": _random_email(),
                "code": "000000",
                "new_password": "brand-new-pw-1",
            },
        )
        assert wrong_code_resp.status_code == unknown_email_resp.status_code == 400
        assert (
            wrong_code_resp.json()["detail"]["error"]
            == unknown_email_resp.json()["detail"]["error"]
            == "password_reset_invalid_or_expired_code"
        )

    async def test_no_code_issued_is_reported_as_invalid_not_a_crash(
        self, client: AsyncClient, create_user
    ):
        user = await create_user()
        resp = await client.post(
            CONFIRM,
            json={
                "email": user.email,
                "code": "123456",
                "new_password": "brand-new-pw-1",
            },
        )
        assert resp.status_code == 400
        assert (
            resp.json()["detail"]["error"] == "password_reset_invalid_or_expired_code"
        )

    async def test_google_linked_account_cannot_reset_a_password(
        self, client: AsyncClient, create_user, db: AsyncSession
    ):
        user = await create_user()
        await _link_google(db, user)
        resp = await client.post(
            CONFIRM,
            json={
                "email": user.email,
                "code": "123456",
                "new_password": "brand-new-pw-1",
            },
        )
        assert resp.status_code == 400
        assert (
            resp.json()["detail"]["error"] == "password_reset_invalid_or_expired_code"
        )

    async def test_too_many_wrong_attempts_locks_out_the_code(
        self, client: AsyncClient, create_user, redis: Redis
    ):
        user = await create_user()
        await pr.issue_code(redis, str(user.id))
        payload = {
            "email": user.email,
            "code": "000000",
            "new_password": "brand-new-pw-1",
        }

        for _ in range(settings.PASSWORD_RESET_MAX_ATTEMPTS):
            assert (await client.post(CONFIRM, json=payload)).status_code == 400

        resp = await client.post(CONFIRM, json=payload)
        assert resp.status_code == 429
        assert resp.json()["detail"]["error"] == "too_many_password_reset_attempts"

    async def test_weak_new_password_does_not_burn_the_code(
        self, client: AsyncClient, create_user, redis: Redis, monkeypatch
    ):
        """A code that checks out but is followed by a rejected weak password
        must still work on an immediate retry - see
        app.core.password_reset.consume_code."""
        monkeypatch.setattr(settings, "REQUIRE_STRONG_PASSWORD", True)
        user = await create_user()
        code = await pr.issue_code(redis, str(user.id))

        weak_resp = await client.post(
            CONFIRM, json={"email": user.email, "code": code, "new_password": "weak"}
        )
        assert weak_resp.status_code == 400
        assert weak_resp.json()["detail"]["error"] == "reset_password_invalid_password"

        strong_resp = await client.post(
            CONFIRM,
            json={
                "email": user.email,
                "code": code,
                "new_password": "Str0ng!Passw0rd",
            },
        )
        assert strong_resp.status_code == 204, strong_resp.text

    async def test_confirm_is_rate_limited_per_ip(
        self, client: AsyncClient, create_user, monkeypatch
    ):
        monkeypatch.setattr(settings, "PASSWORD_RESET_CONFIRM_RATE_LIMIT_PER_IP", 2)
        user = await create_user()
        payload = {
            "email": user.email,
            "code": "000000",
            "new_password": "brand-new-pw-1",
        }

        for _ in range(2):
            assert (await client.post(CONFIRM, json=payload)).status_code == 400

        resp = await client.post(CONFIRM, json=payload)
        assert resp.status_code == 429
        assert resp.json()["detail"]["error"] == "rate_limited"
