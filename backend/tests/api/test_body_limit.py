"""Request bodies are bounded before anything reads them (app/core/body_limit.py).

Two caps by content type - multipart gets the upload one, everything else the
small one - and two layers per cap: the declared `Content-Length` first, then the
bytes as they actually arrive, for a body that declares no length or lies.
"""

from httpx import AsyncClient

from app.core.config import settings

_JSON = {"content-type": "application/json"}


class TestBodySizeLimit:
    async def test_declared_oversize_json_body_is_refused_at_the_door(
        self, client: AsyncClient
    ):
        body = b"x" * (settings.MAX_REQUEST_BODY_BYTES + 1)
        resp = await client.post(
            settings.API_PATH + "/auth/register", content=body, headers=_JSON
        )
        assert resp.status_code == 413
        assert resp.json()["detail"] == {
            "error": "request_too_large",
            "max_bytes": settings.MAX_REQUEST_BODY_BYTES,
        }

    async def test_chunked_oversize_body_is_cut_off_as_it_streams(
        self, client: AsyncClient
    ):
        """No `Content-Length` at all (httpx sends an iterator chunked), so only
        the counting layer can catch it - and it surfaces as the same 413."""
        cap = settings.MAX_REQUEST_BODY_BYTES

        async def chunks():
            for _ in range(4):
                yield b"x" * (cap // 2)

        resp = await client.post(
            settings.API_PATH + "/auth/register", content=chunks(), headers=_JSON
        )
        assert resp.status_code == 413
        assert resp.json()["detail"]["error"] == "request_too_large"

    async def test_multipart_body_gets_the_upload_cap(self, client: AsyncClient):
        """A signed-out feedback submission with an attachment larger than the
        small cap is not refused by the size limit - it may be refused as an
        invalid image, which is the route's own business."""
        blob = b"\x00" * (settings.MAX_REQUEST_BODY_BYTES * 2)
        resp = await client.post(
            settings.API_PATH + "/feedback",
            data={"kind": "feedback", "message": "hi", "consent": "true"},
            files=[("files", ("shot.png", blob, "image/png"))],
        )
        assert resp.status_code != 413, resp.text

    async def test_multipart_body_over_the_upload_cap_is_refused(
        self, client: AsyncClient, monkeypatch
    ):
        monkeypatch.setattr(settings, "MAX_UPLOAD_BODY_BYTES", 1024)
        blob = b"\x00" * 4096
        resp = await client.post(
            settings.API_PATH + "/feedback",
            data={"kind": "feedback", "message": "hi", "consent": "true"},
            files=[("files", ("shot.png", blob, "image/png"))],
        )
        assert resp.status_code == 413
        assert resp.json()["detail"]["max_bytes"] == 1024

    async def test_bodiless_requests_are_untouched(self, client: AsyncClient):
        resp = await client.get(settings.API_PATH + "/health")
        assert resp.status_code == 200

    async def test_refusal_carries_the_request_id(self, client: AsyncClient):
        """The 413 is sent inside the logging middleware, so it is one logged
        request like any other and the header the client needs to report it is
        on the response."""
        body = b"x" * (settings.MAX_REQUEST_BODY_BYTES + 1)
        resp = await client.post(
            settings.API_PATH + "/auth/register", content=body, headers=_JSON
        )
        assert resp.status_code == 413
        assert resp.headers.get("x-request-id")
