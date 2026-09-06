"""What the logging setup promises, pinned.

Three things are contracts rather than implementation detail, because something
outside this repo depends on each of them: the JSON line shape (a log explorer
parses it), the request id (a user reads it off a 500 and support searches for
it), and the field-carrying logger call (every module in the app calls it).
"""

import json
import logging

import pytest
from httpx import AsyncClient
from httpx._transports.asgi import ASGITransport

from app.core import logger as logmod
from app.core import request_logging as reqlog
from app.core.config import settings


@pytest.fixture
def records(monkeypatch):
    """Capture formatted lines from a handler configured like the real one."""

    class Capture(logging.Handler):
        def __init__(self, formatter):
            super().__init__()
            self.setFormatter(formatter)
            self.addFilter(logmod.ContextFilter())
            self.lines: list[str] = []

        def emit(self, record):
            self.lines.append(self.format(record))

    target = logging.getLogger("tests.logging")
    created: list[logging.Handler] = []

    def build(formatter=None):
        handler = Capture(formatter or logmod.JsonFormatter({"env": "test"}))
        target.addHandler(handler)
        target.setLevel(logging.DEBUG)
        target.propagate = False
        created.append(handler)
        return handler

    yield build
    for handler in created:
        target.removeHandler(handler)


class TestJsonFormatter:
    def test_fields_are_flat_top_level_keys(self, records):
        handler = records()
        logmod.get_logger("tests.logging").info("post.created", post_id=7, price=3)

        payload = json.loads(handler.lines[-1])
        assert payload["message"] == "post.created"
        assert payload["level"] == "INFO"
        assert payload["logger"] == "tests.logging"
        assert payload["env"] == "test"
        # Flat, not nested under "fields": Railway indexes top-level keys, so
        # `@post_id:7` only works if the key is where this puts it.
        assert payload["post_id"] == 7
        assert payload["price"] == 3

    def test_none_fields_are_dropped(self, records):
        handler = records()
        logmod.get_logger("tests.logging").info("http.request", route=None, status=200)

        payload = json.loads(handler.lines[-1])
        assert "route" not in payload
        assert payload["status"] == 200

    def test_a_field_cannot_overwrite_a_core_key(self, records):
        handler = records()
        logmod.get_logger("tests.logging").info("evt", level="nonsense", logger="x")

        payload = json.loads(handler.lines[-1])
        assert payload["level"] == "INFO"
        assert payload["logger"] == "tests.logging"
        assert payload["field_level"] == "nonsense"

    def test_exceptions_carry_type_message_and_stack(self, records):
        handler = records()
        try:
            raise RuntimeError("boom")
        except RuntimeError:
            logmod.get_logger("tests.logging").exception("thing.failed", thing_id=1)

        payload = json.loads(handler.lines[-1])
        assert payload["error_type"] == "RuntimeError"
        assert payload["error_message"] == "boom"
        assert "RuntimeError: boom" in payload["stack"]
        assert payload["thing_id"] == 1

    def test_non_serializable_values_do_not_break_the_line(self, records):
        """A UUID or a datetime is the normal case for an id field; the formatter
        must never be the thing that raises while logging."""
        import uuid

        handler = records()
        value = uuid.uuid4()
        logmod.get_logger("tests.logging").info("user.thing", user_id=value)

        assert json.loads(handler.lines[-1])["user_id"] == str(value)

    def test_console_formatter_appends_fields(self, records):
        handler = records(logmod.ConsoleFormatter())
        logmod.get_logger("tests.logging").info("post.created", post_id=7)

        assert "post.created" in handler.lines[-1]
        assert "post_id=7" in handler.lines[-1]


class TestRequestContext:
    def test_context_is_merged_into_every_record(self, records):
        handler = records()
        token = logmod.new_request_context(request_id="abc123", path="/x")
        try:
            logmod.get_logger("tests.logging").info("something.happened")
        finally:
            logmod.reset_request_context(token)

        payload = json.loads(handler.lines[-1])
        assert payload["request_id"] == "abc123"
        assert payload["path"] == "/x"

    def test_binding_later_is_visible_to_the_frame_that_opened_it(self, records):
        """The reason the context is a mutable dict: a route's user dependency
        binds `user_id` long after the middleware opened the context, and the
        access-log line the middleware writes afterwards has to see it."""
        handler = records()
        token = logmod.new_request_context(request_id="abc123")
        try:
            logmod.bind_request_context(user_id="u-1")
            assert logmod.current_request_context()["user_id"] == "u-1"
            logmod.get_logger("tests.logging").info("something.happened")
        finally:
            logmod.reset_request_context(token)

        assert json.loads(handler.lines[-1])["user_id"] == "u-1"

    def test_binding_outside_a_request_does_not_raise(self, records):
        handler = records()
        logmod.bind_request_context(job="backfill")
        logmod.get_logger("tests.logging").info("script.ran")
        assert json.loads(handler.lines[-1])["job"] == "backfill"


class TestClientIp:
    """Which address ends up on a log line, and why it is not a setting."""

    @staticmethod
    def _scope(forwarded: str | None = None) -> dict:
        headers = [(b"x-forwarded-for", forwarded.encode())] if forwarded else []
        return {"type": "http", "client": ("10.0.0.7", 51234), "headers": headers}

    def test_without_a_proxy_the_socket_peer_wins(self):
        """A forwarded-for header with nothing trustworthy in front of us is just
        text the caller sent, so it is ignored outright."""
        scope = self._scope("203.0.113.9")
        assert reqlog._client_ip(scope, behind_proxy=False) == "10.0.0.7"

    def test_behind_a_proxy_the_rightmost_entry_wins(self):
        """A proxy *appends*, so the header is `<what the caller claimed>, <what
        the proxy actually saw>`. Taking the first entry - which this used to do -
        reads back the caller's own invention even though a trusted hop is there.
        """
        scope = self._scope("1.1.1.1, 2.2.2.2, 198.51.100.4")
        assert reqlog._client_ip(scope, behind_proxy=True) == "198.51.100.4"

    def test_a_single_entry_works_for_a_proxy_that_overwrites(self):
        scope = self._scope("198.51.100.4")
        assert reqlog._client_ip(scope, behind_proxy=True) == "198.51.100.4"

    def test_behind_a_proxy_with_no_header_falls_back_to_the_socket(self):
        assert reqlog._client_ip(self._scope(), behind_proxy=True) == "10.0.0.7"

    def test_the_flag_removes_the_field_entirely(self, monkeypatch):
        """LOG_CLIENT_IP is the GDPR opt-out, and it applies to both sources."""
        monkeypatch.setattr(settings, "LOG_CLIENT_IP", False)
        assert reqlog._client_ip(self._scope("198.51.100.4"), behind_proxy=True) is None
        assert reqlog._client_ip(self._scope(), behind_proxy=False) is None


class TestRequestLogging:
    async def test_response_carries_a_request_id(self, client: AsyncClient):
        resp = await client.get(f"{settings.API_PATH}/health")
        assert resp.status_code == 200
        assert resp.headers["x-request-id"]

    async def test_an_inbound_request_id_is_reused(self, client: AsyncClient):
        """So a client-side breadcrumb and the server's lines share one id."""
        resp = await client.get(
            f"{settings.API_PATH}/health", headers={"X-Request-ID": "client-side-id"}
        )
        assert resp.headers["x-request-id"] == "client-side-id"

    async def test_one_line_per_request_with_status_and_duration(
        self, app, caplog
    ) -> None:
        with caplog.at_level(logging.DEBUG, logger="app.core.request_logging"):
            transport = ASGITransport(app=app)
            async with AsyncClient(
                transport=transport, base_url="http://test"
            ) as client:
                await client.get(f"{settings.API_PATH}/hello-world")

        lines = [r for r in caplog.records if r.getMessage() == "http.request"]
        assert len(lines) == 1
        fields = lines[0].fields
        assert fields["status"] == 200
        assert fields["route"] == f"{settings.API_PATH}/hello-world"
        assert fields["duration_ms"] >= 0

    async def test_polled_endpoints_are_logged_at_debug(self, app, caplog) -> None:
        """`/health` is hit by Railway's probe and by the client's reconnect loop;
        at INFO it would be most of the log and none of the information."""
        with caplog.at_level(logging.DEBUG, logger="app.core.request_logging"):
            transport = ASGITransport(app=app)
            async with AsyncClient(
                transport=transport, base_url="http://test"
            ) as client:
                await client.get(f"{settings.API_PATH}/health")

        line = next(r for r in caplog.records if r.getMessage() == "http.request")
        assert line.levelno == logging.DEBUG

    async def test_a_failing_request_logs_the_traceback_with_its_id(
        self, app, create_user, monkeypatch, caplog
    ) -> None:
        """The whole point of the exercise: the id in the 500 body finds the
        traceback, and the traceback is written once."""
        from app.feed import service
        from tests.utils import get_jwt_header

        user = await create_user()

        async def boom(*args, **kwargs):
            raise RuntimeError("something exploded")

        monkeypatch.setattr(service, "render_queue_ids", boom)
        with caplog.at_level(logging.ERROR, logger="app.core.request_logging"):
            transport = ASGITransport(app=app, raise_app_exceptions=False)
            async with AsyncClient(
                transport=transport, base_url="http://test"
            ) as client:
                resp = await client.get(
                    f"{settings.API_PATH}/posts/feed", headers=get_jwt_header(user)
                )

        request_id = resp.json()["detail"]["request_id"]
        failures = [
            r for r in caplog.records if r.getMessage() == "http.request_failed"
        ]
        assert len(failures) == 1
        assert failures[0].context["request_id"] == request_id
        assert failures[0].exc_info is not None
        # The user the request was made as is on the line too, bound by the
        # dependency rather than passed in by the route.
        assert failures[0].context["user_id"] == str(user.id)
