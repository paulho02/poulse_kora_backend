from httpx import AsyncClient

from app.core.config import settings
from app.models.item import Item
from app.models.user import User
from tests.utils import get_jwt_header


class TestGetItems:
    async def test_get_items_not_logged_in(self, client: AsyncClient):
        resp = await client.get(settings.API_PATH + "/items")
        assert resp.status_code == 401

    async def test_get_items(self, client: AsyncClient, create_user, create_item):
        user: User = await create_user()
        await create_item(user=user)
        jwt_header = get_jwt_header(user)
        resp = await client.get(settings.API_PATH + "/items", headers=jwt_header)
        assert resp.status_code == 200
        assert resp.headers["Content-Range"] == "0-1/1"
        assert len(resp.json()) == 1

    async def test_get_items_only_returns_own_items(
        self, client: AsyncClient, create_user, create_item
    ):
        owner: User = await create_user()
        other: User = await create_user()
        item: Item = await create_item(user=owner)
        await create_item(user=other)

        resp = await client.get(
            settings.API_PATH + "/items", headers=get_jwt_header(owner)
        )
        assert resp.status_code == 200, resp.text
        ids = {i["id"] for i in resp.json()}
        assert ids == {item.id}

    async def test_get_items_respects_range_query_param(
        self, client: AsyncClient, create_user, create_item
    ):
        user: User = await create_user()
        for _ in range(3):
            await create_item(user=user)
        resp = await client.get(
            settings.API_PATH + "/items",
            params={"range": "[0,1]"},
            headers=get_jwt_header(user),
        )
        assert resp.status_code == 200, resp.text
        assert resp.headers["Content-Range"] == "0-2/3"
        assert len(resp.json()) == 2

    async def test_get_items_invalid_sort_direction_is_400(
        self, client: AsyncClient, create_user
    ):
        user: User = await create_user()
        resp = await client.get(
            settings.API_PATH + "/items",
            params={"sort": '["id","sideways"]'},
            headers=get_jwt_header(user),
        )
        assert resp.status_code == 400

    async def test_get_items_respects_sort_asc(
        self, client: AsyncClient, create_user, create_item
    ):
        user: User = await create_user()
        first: Item = await create_item(user=user)
        second: Item = await create_item(user=user)
        resp = await client.get(
            settings.API_PATH + "/items",
            params={"sort": '["id","ASC"]'},
            headers=get_jwt_header(user),
        )
        assert resp.status_code == 200, resp.text
        ids = [i["id"] for i in resp.json()]
        assert ids == sorted([first.id, second.id])

    async def test_get_items_respects_sort_desc(
        self, client: AsyncClient, create_user, create_item
    ):
        user: User = await create_user()
        first: Item = await create_item(user=user)
        second: Item = await create_item(user=user)
        resp = await client.get(
            settings.API_PATH + "/items",
            params={"sort": '["id","DESC"]'},
            headers=get_jwt_header(user),
        )
        assert resp.status_code == 200, resp.text
        ids = [i["id"] for i in resp.json()]
        assert ids == sorted([first.id, second.id], reverse=True)


class TestGetSingleItem:
    async def test_get_single_item(self, client: AsyncClient, create_user, create_item):
        user: User = await create_user()
        item: Item = await create_item(user=user)
        jwt_header = get_jwt_header(user)
        resp = await client.get(
            settings.API_PATH + f"/items/{item.id}", headers=jwt_header
        )
        assert resp.status_code == 200, resp.text
        data = resp.json()
        assert data["id"] == item.id
        assert data["value"] == item.value

    async def test_get_single_item_does_not_exist(
        self, client: AsyncClient, create_user
    ):
        user: User = await create_user()
        resp = await client.get(
            settings.API_PATH + f"/items/{10**6}", headers=get_jwt_header(user)
        )
        assert resp.status_code == 404, resp.text

    async def test_get_single_item_belonging_to_another_user_is_404(
        self, client: AsyncClient, create_user, create_item
    ):
        owner: User = await create_user()
        other: User = await create_user()
        item: Item = await create_item(user=owner)
        resp = await client.get(
            settings.API_PATH + f"/items/{item.id}", headers=get_jwt_header(other)
        )
        assert resp.status_code == 404, resp.text


class TestCreateItem:
    async def test_create_item(self, client: AsyncClient, create_user):
        user: User = await create_user()
        jwt_header = get_jwt_header(user)

        resp = await client.post(
            settings.API_PATH + "/items", headers=jwt_header, json={"value": "value"}
        )
        assert resp.status_code == 201, resp.text
        assert resp.json()["id"]

    async def test_create_item_not_logged_in(self, client: AsyncClient):
        resp = await client.post(
            settings.API_PATH + "/items", json={"value": "value"}
        )
        assert resp.status_code == 401


class TestDeleteItem:
    async def test_delete_item(self, client: AsyncClient, create_user, create_item):
        user: User = await create_user()
        item: Item = await create_item(user=user)
        jwt_header = get_jwt_header(user)

        resp = await client.delete(
            settings.API_PATH + f"/items/{item.id}", headers=jwt_header
        )
        assert resp.status_code == 200

    async def test_delete_item_does_not_exist(self, client: AsyncClient, create_user):
        user: User = await create_user()
        jwt_header = get_jwt_header(user)

        resp = await client.delete(
            settings.API_PATH + f"/items/{10**6}", headers=jwt_header
        )
        assert resp.status_code == 404, resp.text

    async def test_delete_item_belonging_to_another_user_is_404(
        self, client: AsyncClient, create_user, create_item
    ):
        owner: User = await create_user()
        other: User = await create_user()
        item: Item = await create_item(user=owner)
        resp = await client.delete(
            settings.API_PATH + f"/items/{item.id}", headers=get_jwt_header(other)
        )
        assert resp.status_code == 404, resp.text

        # Never actually deleted.
        resp = await client.get(
            settings.API_PATH + f"/items/{item.id}", headers=get_jwt_header(owner)
        )
        assert resp.status_code == 200


class TestUpdateItem:
    async def test_update_item(self, client: AsyncClient, create_user, create_item):
        user: User = await create_user()
        item: Item = await create_item(user=user)
        jwt_header = get_jwt_header(user)

        resp = await client.put(
            settings.API_PATH + f"/items/{item.id}",
            headers=jwt_header,
            json={"value": "new value"},
        )
        assert resp.status_code == 200, resp.text
        assert resp.json()["value"] == "new value"

    async def test_update_item_does_not_exist(self, client: AsyncClient, create_user):
        user: User = await create_user()
        resp = await client.put(
            settings.API_PATH + f"/items/{10**6}",
            headers=get_jwt_header(user),
            json={"value": "new value"},
        )
        assert resp.status_code == 404, resp.text

    async def test_update_item_belonging_to_another_user_is_404(
        self, client: AsyncClient, create_user, create_item
    ):
        owner: User = await create_user()
        other: User = await create_user()
        item: Item = await create_item(user=owner)
        resp = await client.put(
            settings.API_PATH + f"/items/{item.id}",
            headers=get_jwt_header(other),
            json={"value": "hijacked"},
        )
        assert resp.status_code == 404, resp.text
