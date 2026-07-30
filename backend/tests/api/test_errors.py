"""The error envelope contract: every error body is
`{"detail": {"error": <code>, ...}}`.

The Flutter client keys all of its user-facing copy off `detail.error`
(`lib/src/core/errors/error_messages.dart`), so a response that falls back to
FastAPI's default string detail silently degrades to a generic "something went
wrong" in the app. These tests pin the shape rather than the prose.
"""

from collections.abc import Callable

from httpx import AsyncClient
from httpx._transports.asgi import ASGITransport
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.feed import service
from tests.utils import get_jwt_header


async def test_health_check_needs_no_auth(client: AsyncClient) -> None:
    resp = await client.get(f"{settings.API_PATH}/health")
    assert resp.status_code == 200
    assert resp.json()["msg"] == "ok"


class TestErrorEnvelope:
    async def test_route_404_uses_named_code(
        self, client: AsyncClient, create_user: Callable
    ) -> None:
        user = await create_user()
        resp = await client.get(
            f"{settings.API_PATH}/posts/999999", headers=get_jwt_header(user)
        )
        assert resp.status_code == 404
        assert resp.json()["detail"] == {"error": "post_not_found"}

    async def test_unmatched_route_404_is_still_structured(
        self, client: AsyncClient
    ) -> None:
        """Starlette raises this with a bare string detail; the handler slugifies it."""
        resp = await client.get(f"{settings.API_PATH}/no-such-endpoint")
        assert resp.status_code == 404
        detail = resp.json()["detail"]
        assert detail["error"] == "not_found"
        assert detail["message"] == "Not Found"

    async def test_missing_auth_is_structured(self, client: AsyncClient) -> None:
        resp = await client.get(f"{settings.API_PATH}/posts/feed")
        assert resp.status_code == 401
        assert resp.json()["detail"]["error"] == "unauthorized"

    async def test_validation_error_lists_fields(
        self, client: AsyncClient, create_user: Callable
    ) -> None:
        user = await create_user()
        resp = await client.post(
            f"{settings.API_PATH}/posts",
            json={"text": "no channel_id"},
            headers=get_jwt_header(user),
        )
        assert resp.status_code == 422
        detail = resp.json()["detail"]
        assert detail["error"] == "validation_error"
        assert "channel_id" in {field["field"] for field in detail["fields"]}

    async def test_structured_detail_passes_through(
        self,
        client: AsyncClient,
        create_user: Callable,
        create_channel: Callable,
    ) -> None:
        """A route that already raises a dict detail keeps its extra context."""
        user = await create_user()
        channel = await create_channel()
        resp = await client.post(
            f"{settings.API_PATH}/posts",
            json={"channel_id": channel.id, "text": "costs tokens"},
            headers=get_jwt_header(user),
        )
        assert resp.status_code == 402
        detail = resp.json()["detail"]
        assert detail["error"] == "insufficient_tokens"
        # The client renders these two directly ("need 5, you have 0").
        assert detail["balance"] == 0
        assert detail["price"] > 0

    async def test_unhandled_exception_is_a_structured_500(
        self,
        app,
        create_user: Callable,
        monkeypatch,
    ) -> None:
        """See app.factory.setup_exception_handlers: the client must never see a
        raw traceback or FastAPI's default error shape for a genuine bug.

        Uses a one-off client with raise_app_exceptions=False: ASGITransport's
        default re-raises after the response is sent (so a real ASGI server can log
        it), which would otherwise surface here as the raw RuntimeError instead of
        the response we want to assert on."""
        user = await create_user()

        async def boom(*args, **kwargs):
            raise RuntimeError("something exploded")

        monkeypatch.setattr(service, "render_queue_ids", boom)
        transport = ASGITransport(app=app, raise_app_exceptions=False)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            resp = await client.get(
                f"{settings.API_PATH}/posts/feed", headers=get_jwt_header(user)
            )
        assert resp.status_code == 500
        assert resp.json()["detail"] == {"error": "internal_error"}


class TestSettingsRevision:
    """`settings_revision` lets the app decide push-vs-pull after being offline."""

    async def test_starts_at_zero_and_is_exposed(
        self, client: AsyncClient, create_user: Callable
    ) -> None:
        user = await create_user()
        resp = await client.get(
            f"{settings.API_PATH}/users/me", headers=get_jwt_header(user)
        )
        assert resp.status_code == 200
        assert resp.json()["settings_revision"] == 0

    async def test_changing_a_setting_bumps_it(
        self, client: AsyncClient, create_user: Callable
    ) -> None:
        user = await create_user()
        headers = get_jwt_header(user)
        resp = await client.patch(
            f"{settings.API_PATH}/users/me", json={"dark_mode": True}, headers=headers
        )
        assert resp.status_code == 200
        assert resp.json()["settings_revision"] == 1

    async def test_resending_the_same_value_does_not_bump_it(
        self, client: AsyncClient, create_user: Callable
    ) -> None:
        """Otherwise an idempotent retry after a flaky connection would look to the
        next device like a competing change."""
        user = await create_user()
        headers = get_jwt_header(user)
        await client.patch(
            f"{settings.API_PATH}/users/me", json={"dark_mode": True}, headers=headers
        )
        resp = await client.patch(
            f"{settings.API_PATH}/users/me", json={"dark_mode": True}, headers=headers
        )
        assert resp.json()["settings_revision"] == 1

    async def test_non_settings_field_does_not_bump_it(
        self, client: AsyncClient, create_user: Callable
    ) -> None:
        user = await create_user()
        resp = await client.patch(
            f"{settings.API_PATH}/users/me",
            json={"bio": "hello"},
            headers=get_jwt_header(user),
        )
        assert resp.json()["bio"] == "hello"
        assert resp.json()["settings_revision"] == 0

    async def test_reviewing_a_post_does_not_bump_it(
        self,
        client: AsyncClient,
        db: AsyncSession,
        redis,
        create_user: Callable,
        create_channel: Callable,
        create_post: Callable,
    ) -> None:
        """The reason this column exists instead of reusing `User.updated`, which
        moves on every review because reviewed_count is on the same row."""
        from app.feed import service

        user = await create_user()
        channel = await create_channel()
        post = await create_post(channel=channel)
        await service.place_post(redis, str(user.id), post.id)

        resp = await client.post(
            f"{settings.API_PATH}/posts/{post.id}/review",
            json={"kind": "forward"},
            headers=get_jwt_header(user),
        )
        assert resp.status_code == 200

        me = await client.get(
            f"{settings.API_PATH}/users/me", headers=get_jwt_header(user)
        )
        assert me.json()["settings_revision"] == 0
