from httpx import AsyncClient

from app.core.config import settings
from app.models.user import User
from tests.utils import get_jwt_header


class TestChangePassword:
    async def test_wrong_current_password_is_rejected(
        self, client: AsyncClient, create_user
    ):
        user: User = await create_user()
        resp = await client.post(
            settings.API_PATH + "/auth/change-password",
            json={"current_password": "definitely-not-it", "new_password": "whatever-new-1"},
            headers=get_jwt_header(user),
        )
        assert resp.status_code == 400
        assert resp.json()["detail"]["error"] == "change_password_wrong_current_password"

    async def test_success_changes_password_and_can_login_with_new(
        self, client: AsyncClient, create_user, default_password: str
    ):
        user: User = await create_user()
        resp = await client.post(
            settings.API_PATH + "/auth/change-password",
            json={"current_password": default_password, "new_password": "brand-new-pw-1"},
            headers=get_jwt_header(user),
        )
        assert resp.status_code == 204, resp.text

        # Old password no longer works.
        resp = await client.post(
            settings.API_PATH + "/auth/jwt/login",
            data={"username": user.email, "password": default_password},
        )
        assert resp.status_code == 400

        # New password does.
        resp = await client.post(
            settings.API_PATH + "/auth/jwt/login",
            data={"username": user.email, "password": "brand-new-pw-1"},
        )
        assert resp.status_code == 200, resp.text
        assert "access_token" in resp.json()

    async def test_weak_new_password_is_rejected_with_a_reason(
        self, client: AsyncClient, create_user, default_password: str, monkeypatch
    ):
        monkeypatch.setattr(settings, "REQUIRE_STRONG_PASSWORD", True)
        user: User = await create_user()
        resp = await client.post(
            settings.API_PATH + "/auth/change-password",
            json={"current_password": default_password, "new_password": "weak"},
            headers=get_jwt_header(user),
        )
        assert resp.status_code == 400
        detail = resp.json()["detail"]
        assert detail["error"] == "change_password_invalid_password"
        codes = {v["code"] for v in detail["reason"]}
        assert codes == {"password_too_short", "password_missing_variety"}

    async def test_weak_new_password_is_accepted_when_policy_off(
        self, client: AsyncClient, create_user, default_password: str
    ):
        assert settings.REQUIRE_STRONG_PASSWORD is False
        user: User = await create_user()
        resp = await client.post(
            settings.API_PATH + "/auth/change-password",
            json={"current_password": default_password, "new_password": "weak"},
            headers=get_jwt_header(user),
        )
        assert resp.status_code == 204, resp.text

    async def test_rate_limited_after_too_many_attempts(
        self, client: AsyncClient, create_user, monkeypatch
    ):
        monkeypatch.setattr(settings, "PASSWORD_CHANGE_RATE_LIMIT", 2)
        user: User = await create_user()
        header = get_jwt_header(user)
        payload = {"current_password": "wrong", "new_password": "whatever-new-1"}

        for _ in range(2):
            resp = await client.post(
                settings.API_PATH + "/auth/change-password", json=payload, headers=header
            )
            assert resp.status_code == 400  # wrong current password, but spends a slot

        resp = await client.post(
            settings.API_PATH + "/auth/change-password", json=payload, headers=header
        )
        assert resp.status_code == 429, resp.text
        assert resp.json()["detail"]["error"] == "rate_limited"
        assert "Retry-After" in resp.headers

    async def test_superuser_is_exempt_from_rate_limit(
        self, client: AsyncClient, db, create_user, monkeypatch
    ):
        monkeypatch.setattr(settings, "PASSWORD_CHANGE_RATE_LIMIT", 1)
        user: User = await create_user()
        user.is_superuser = True
        db.add(user)
        await db.commit()
        header = get_jwt_header(user)
        payload = {"current_password": "wrong", "new_password": "whatever-new-1"}

        for _ in range(3):
            resp = await client.post(
                settings.API_PATH + "/auth/change-password", json=payload, headers=header
            )
            assert resp.status_code == 400


class TestPatchUsersMeRejectsPassword:
    """PATCH /users/me must never be a password-change bypass — see
    UserManager.update in app/deps/users.py."""

    async def test_password_field_is_rejected(self, client: AsyncClient, create_user):
        user: User = await create_user()
        resp = await client.patch(
            settings.API_PATH + "/users/me",
            json={"password": "some-new-password-1"},
            headers=get_jwt_header(user),
        )
        assert resp.status_code == 400
        assert resp.json()["detail"]["error"] == "update_user_invalid_password"

    async def test_other_fields_still_update(self, client: AsyncClient, create_user):
        user: User = await create_user()
        resp = await client.patch(
            settings.API_PATH + "/users/me",
            json={"bio": "still works"},
            headers=get_jwt_header(user),
        )
        assert resp.status_code == 200, resp.text
        assert resp.json()["bio"] == "still works"
