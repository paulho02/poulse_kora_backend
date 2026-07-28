"""REQUIRE_EMAIL_VERIFICATION gating and the code confirm/resend endpoints.

See CLAUDE.md / app.core.email_verification for why this is a short code stored in
Redis rather than fastapi-users' own link-based verify flow.
"""

from collections.abc import Callable

from httpx import AsyncClient
from redis.asyncio import Redis

from app.core import email_verification as ev
from app.core.config import settings
from tests.utils import get_jwt_header


class TestVerificationGate:
    async def test_unverified_user_is_blocked_from_feed_actions(
        self, client: AsyncClient, create_user: Callable
    ) -> None:
        user = await create_user(is_verified=False)
        resp = await client.get(
            f"{settings.API_PATH}/posts/feed", headers=get_jwt_header(user)
        )
        assert resp.status_code == 403
        assert resp.json()["detail"] == {"error": "unverified_user"}

    async def test_verified_user_is_not_blocked(
        self, client: AsyncClient, create_user: Callable
    ) -> None:
        user = await create_user(is_verified=True)
        resp = await client.get(
            f"{settings.API_PATH}/posts/feed", headers=get_jwt_header(user)
        )
        assert resp.status_code == 200

    async def test_own_profile_stays_readable_while_unverified(
        self, client: AsyncClient, create_user: Callable
    ) -> None:
        """The client needs this to even find out it's unverified."""
        user = await create_user(is_verified=False)
        resp = await client.get(
            f"{settings.API_PATH}/users/me", headers=get_jwt_header(user)
        )
        assert resp.status_code == 200
        assert resp.json()["is_verified"] is False


class TestConfirmEmailVerification:
    async def test_correct_code_verifies_the_account(
        self, client: AsyncClient, create_user: Callable, redis: Redis
    ) -> None:
        user = await create_user(is_verified=False)
        code = await ev.issue_code(redis, str(user.id))

        resp = await client.post(
            f"{settings.API_PATH}/auth/email-verification/confirm",
            json={"code": code},
            headers=get_jwt_header(user),
        )
        assert resp.status_code == 200, resp.text
        assert resp.json()["is_verified"] is True

        # And the gate actually lifts.
        feed_resp = await client.get(
            f"{settings.API_PATH}/posts/feed", headers=get_jwt_header(user)
        )
        assert feed_resp.status_code == 200

    async def test_wrong_code_reports_attempts_remaining(
        self, client: AsyncClient, create_user: Callable, redis: Redis
    ) -> None:
        user = await create_user(is_verified=False)
        await ev.issue_code(redis, str(user.id))

        resp = await client.post(
            f"{settings.API_PATH}/auth/email-verification/confirm",
            json={"code": "000000"},
            headers=get_jwt_header(user),
        )
        assert resp.status_code == 400
        detail = resp.json()["detail"]
        assert detail["error"] == "invalid_verification_code"
        expected_remaining = settings.EMAIL_VERIFICATION_MAX_ATTEMPTS - 1
        assert detail["attempts_remaining"] == expected_remaining

    async def test_no_code_issued_is_expired_not_a_crash(
        self, client: AsyncClient, create_user: Callable
    ) -> None:
        user = await create_user(is_verified=False)
        resp = await client.post(
            f"{settings.API_PATH}/auth/email-verification/confirm",
            json={"code": "123456"},
            headers=get_jwt_header(user),
        )
        assert resp.status_code == 400
        assert resp.json()["detail"]["error"] == "verification_code_expired"

    async def test_too_many_wrong_attempts_locks_out_the_code(
        self, client: AsyncClient, create_user: Callable, redis: Redis
    ) -> None:
        user = await create_user(is_verified=False)
        await ev.issue_code(redis, str(user.id))
        headers = get_jwt_header(user)

        for _ in range(settings.EMAIL_VERIFICATION_MAX_ATTEMPTS):
            resp = await client.post(
                f"{settings.API_PATH}/auth/email-verification/confirm",
                json={"code": "000000"},
                headers=headers,
            )
            assert resp.status_code == 400

        resp = await client.post(
            f"{settings.API_PATH}/auth/email-verification/confirm",
            json={"code": "000000"},
            headers=headers,
        )
        assert resp.status_code == 429
        assert resp.json()["detail"]["error"] == "too_many_verification_attempts"

    async def test_already_verified_is_a_no_op_success(
        self, client: AsyncClient, create_user: Callable
    ) -> None:
        user = await create_user(is_verified=True)
        resp = await client.post(
            f"{settings.API_PATH}/auth/email-verification/confirm",
            json={"code": "000000"},
            headers=get_jwt_header(user),
        )
        assert resp.status_code == 200
        assert resp.json()["is_verified"] is True


class TestResendEmailVerification:
    async def test_resend_issues_a_new_code_and_sets_cooldown(
        self, client: AsyncClient, create_user: Callable, redis: Redis
    ) -> None:
        user = await create_user(is_verified=False)
        resp = await client.post(
            f"{settings.API_PATH}/auth/email-verification/resend",
            headers=get_jwt_header(user),
        )
        assert resp.status_code == 200
        assert resp.json()["is_verified"] is False
        assert await ev.resend_cooldown_remaining(redis, str(user.id)) > 0

    async def test_resend_within_cooldown_is_rate_limited(
        self, client: AsyncClient, create_user: Callable
    ) -> None:
        user = await create_user(is_verified=False)
        headers = get_jwt_header(user)
        await client.post(
            f"{settings.API_PATH}/auth/email-verification/resend", headers=headers
        )
        resp = await client.post(
            f"{settings.API_PATH}/auth/email-verification/resend", headers=headers
        )
        assert resp.status_code == 429
        detail = resp.json()["detail"]
        assert detail["error"] == "resend_cooldown"
        assert detail["retry_after"] > 0
        assert "Retry-After" in resp.headers

    async def test_resend_when_already_verified_is_a_no_op(
        self, client: AsyncClient, create_user: Callable
    ) -> None:
        user = await create_user(is_verified=True)
        resp = await client.post(
            f"{settings.API_PATH}/auth/email-verification/resend",
            headers=get_jwt_header(user),
        )
        assert resp.status_code == 200
        assert resp.json()["is_verified"] is True


class TestPublicConfig:
    async def test_exposes_the_verification_and_password_flags(
        self, client: AsyncClient
    ) -> None:
        resp = await client.get(f"{settings.API_PATH}/config")
        assert resp.status_code == 200
        body = resp.json()
        assert body["require_email_verification"] == settings.REQUIRE_EMAIL_VERIFICATION
        assert body["require_strong_password"] == settings.REQUIRE_STRONG_PASSWORD
        assert (
            body["email_verification_resend_cooldown_seconds"]
            == settings.EMAIL_VERIFICATION_RESEND_COOLDOWN_SECONDS
        )
