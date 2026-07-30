"""Unit tests for app.factory helpers that aren't naturally exercised by hitting
API routes (build-time invariants, non-API fallback behavior)."""

import pytest
from fastapi import FastAPI
from fastapi.routing import APIRoute

from app.factory import serve_static_app, use_route_names_as_operation_ids


class TestUseRouteNamesAsOperationIds:
    def test_raises_on_duplicate_route_names(self):
        app = FastAPI()

        @app.get("/a", name="dup")
        async def _a():
            return {}

        @app.get("/b", name="dup")
        async def _b():
            return {}

        with pytest.raises(Exception, match="Route function names should be unique"):
            use_route_names_as_operation_ids(app)

    def test_unique_names_pass_through(self):
        app = FastAPI()

        @app.get("/a", name="a")
        async def _a():
            return {}

        @app.get("/b", name="b")
        async def _b():
            return {}

        use_route_names_as_operation_ids(app)
        operation_ids = {
            route.operation_id for route in app.routes if isinstance(route, APIRoute)
        }
        assert operation_ids == {"a", "b"}


class TestServeStaticApp:
    """The static-file fallback is a vestige of the (disabled) React Admin
    frontend - see CLAUDE.md - but it's still live code, so its 404-vs-passthrough
    behavior stays worth pinning."""

    async def test_unmatched_non_api_path_serves_the_spa_fallback(self, tmp_path):
        app = FastAPI()
        static_dir = tmp_path / "static"
        static_dir.mkdir()
        (static_dir / "index.html").write_text("<html>spa</html>")
        import os

        cwd = os.getcwd()
        os.chdir(tmp_path)
        try:
            serve_static_app(app)
            from httpx import ASGITransport, AsyncClient

            transport = ASGITransport(app=app)
            async with AsyncClient(
                transport=transport, base_url="http://test"
            ) as client:
                resp = await client.get("/some-unknown-page")
            assert resp.status_code == 200
            assert resp.text == "<html>spa</html>"
        finally:
            os.chdir(cwd)

    async def test_api_path_404_is_not_redirected_to_the_spa(self, tmp_path):
        app = FastAPI()
        static_dir = tmp_path / "static"
        static_dir.mkdir()
        (static_dir / "index.html").write_text("<html>spa</html>")
        import os

        cwd = os.getcwd()
        os.chdir(tmp_path)
        try:
            serve_static_app(app)
            from httpx import ASGITransport, AsyncClient

            transport = ASGITransport(app=app)
            async with AsyncClient(
                transport=transport, base_url="http://test"
            ) as client:
                resp = await client.get("/api/v1/no-such-route")
            assert resp.status_code == 404
            assert resp.text != "<html>spa</html>"
        finally:
            os.chdir(cwd)

    async def test_non_404_response_passes_through_unchanged(self, tmp_path):
        """A normal successful response outside /api and /docs must not be swapped
        for the SPA page either - only an actual 404 triggers the fallback."""
        app = FastAPI()
        static_dir = tmp_path / "static"
        static_dir.mkdir()
        (static_dir / "index.html").write_text("<html>spa</html>")

        @app.get("/not-api/ok")
        async def _ok():
            return {"msg": "ok"}

        import os

        cwd = os.getcwd()
        os.chdir(tmp_path)
        try:
            serve_static_app(app)
            from httpx import ASGITransport, AsyncClient

            transport = ASGITransport(app=app)
            async with AsyncClient(
                transport=transport, base_url="http://test"
            ) as client:
                resp = await client.get("/not-api/ok")
            assert resp.status_code == 200
            assert resp.json() == {"msg": "ok"}
        finally:
            os.chdir(cwd)
