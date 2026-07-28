from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.models.user import User
from tests.utils import get_jwt_header


class TestGetBanner:
    async def test_get_when_unset(self, client: AsyncClient):
        resp = await client.get(settings.API_PATH + "/banner")
        assert resp.status_code == 200, resp.text
        assert resp.json() is None


class TestSetBanner:
    async def test_superuser_sets_and_get_reflects_it(
        self, client: AsyncClient, db: AsyncSession, create_user
    ):
        user: User = await create_user()
        user.is_superuser = True
        db.add(user)
        await db.commit()

        resp = await client.post(
            settings.API_PATH + "/banner",
            headers=get_jwt_header(user),
            json={"message": "maintenance tonight"},
        )
        assert resp.status_code == 200, resp.text
        set_body = resp.json()
        assert set_body["message"] == "maintenance tonight"

        get_resp = await client.get(settings.API_PATH + "/banner")
        assert get_resp.json()["id"] == set_body["id"]

    async def test_non_superuser_rejected(self, client: AsyncClient, create_user):
        user: User = await create_user()
        resp = await client.post(
            settings.API_PATH + "/banner",
            headers=get_jwt_header(user),
            json={"message": "should not be allowed"},
        )
        assert resp.status_code == 403

    async def test_anonymous_rejected(self, client: AsyncClient):
        resp = await client.post(
            settings.API_PATH + "/banner", json={"message": "should not be allowed"}
        )
        assert resp.status_code == 401


class TestClearBanner:
    async def test_superuser_clears_it(
        self, client: AsyncClient, db: AsyncSession, create_user
    ):
        user: User = await create_user()
        user.is_superuser = True
        db.add(user)
        await db.commit()

        await client.post(
            settings.API_PATH + "/banner",
            headers=get_jwt_header(user),
            json={"message": "temporary"},
        )
        resp = await client.delete(
            settings.API_PATH + "/banner", headers=get_jwt_header(user)
        )
        assert resp.status_code == 204

        get_resp = await client.get(settings.API_PATH + "/banner")
        assert get_resp.json() is None

    async def test_non_superuser_rejected(self, client: AsyncClient, create_user):
        user: User = await create_user()
        resp = await client.delete(
            settings.API_PATH + "/banner", headers=get_jwt_header(user)
        )
        assert resp.status_code == 403
