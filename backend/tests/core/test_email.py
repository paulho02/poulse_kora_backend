"""app.core.email.send_email: which connector it dispatches to, and what each one
puts on the wire.

Neither connector is reachable by running the rest of the suite. SMTP needs
SMTP_HOST, which is never set in local dev/test config (see CLAUDE.md), and
Lettermint needs both EMAIL_PROVIDER and a token. Both are mocked here - hitting a
real relay or a real provider from a test run is not practical."""

import logging
from unittest.mock import AsyncMock

import pytest
from lettermint.exceptions import HttpRequestError
from pydantic import ValidationError

from app.core import email
from app.core.config import settings


class TestSendEmail:
    async def test_not_configured_logs_and_does_not_send(self, monkeypatch):
        monkeypatch.setattr(settings, "SMTP_HOST", None)
        send_mock = AsyncMock()
        monkeypatch.setattr(email.aiosmtplib, "send", send_mock)

        await email.send_email("to@example.com", "subject", "body")

        send_mock.assert_not_called()

    async def test_configured_sends_via_aiosmtplib_with_expected_fields(
        self, monkeypatch
    ):
        monkeypatch.setattr(settings, "SMTP_HOST", "smtp.example.com")
        monkeypatch.setattr(settings, "SMTP_PORT", 2525)
        monkeypatch.setattr(settings, "SMTP_USERNAME", "user")
        monkeypatch.setattr(settings, "SMTP_PASSWORD", "pass")
        monkeypatch.setattr(settings, "SMTP_USE_TLS", True)
        monkeypatch.setattr(settings, "SMTP_FROM_EMAIL", "no-reply@poulse.com")
        monkeypatch.setattr(settings, "SMTP_FROM_NAME", "Poulse Kora")

        send_mock = AsyncMock()
        monkeypatch.setattr(email.aiosmtplib, "send", send_mock)

        await email.send_email("to@example.com", "hi", "body text")

        send_mock.assert_awaited_once()
        message = send_mock.await_args.args[0]
        assert message["To"] == "to@example.com"
        assert message["Subject"] == "hi"
        assert message["From"] == "Poulse Kora <no-reply@poulse.com>"
        assert message.get_content().strip() == "body text"

        kwargs = send_mock.await_args.kwargs
        assert kwargs["hostname"] == "smtp.example.com"
        assert kwargs["port"] == 2525
        assert kwargs["username"] == "user"
        assert kwargs["password"] == "pass"
        assert kwargs["start_tls"] is True

    async def test_html_becomes_a_multipart_alternative(self, monkeypatch):
        """Text first, HTML second - a client renders the *last* part it can
        display, and a text-only client still gets a readable code."""
        monkeypatch.setattr(settings, "SMTP_HOST", "smtp.example.com")
        send_mock = AsyncMock()
        monkeypatch.setattr(email.aiosmtplib, "send", send_mock)

        await email.send_email("to@example.com", "hi", "body text", "<p>body</p>")

        message = send_mock.await_args.args[0]
        assert message.get_content_type() == "multipart/alternative"
        parts = message.get_payload()
        assert [p.get_content_type() for p in parts] == ["text/plain", "text/html"]
        assert parts[0].get_content().strip() == "body text"
        assert "<p>body</p>" in parts[1].get_content()

    async def test_without_html_the_message_stays_plain_text(self, monkeypatch):
        monkeypatch.setattr(settings, "SMTP_HOST", "smtp.example.com")
        send_mock = AsyncMock()
        monkeypatch.setattr(email.aiosmtplib, "send", send_mock)

        await email.send_email("to@example.com", "hi", "body text")

        assert send_mock.await_args.args[0].get_content_type() == "text/plain"

    async def test_blank_smtp_credentials_are_passed_as_none(self, monkeypatch):
        """Falsy-but-set (empty string) credentials must not be handed to
        aiosmtplib as empty strings, which it treats differently from "no auth"."""
        monkeypatch.setattr(settings, "SMTP_HOST", "smtp.example.com")
        monkeypatch.setattr(settings, "SMTP_USERNAME", "")
        monkeypatch.setattr(settings, "SMTP_PASSWORD", "")

        send_mock = AsyncMock()
        monkeypatch.setattr(email.aiosmtplib, "send", send_mock)

        await email.send_email("to@example.com", "hi", "body")

        kwargs = send_mock.await_args.kwargs
        assert kwargs["username"] is None
        assert kwargs["password"] is None


class _FakeEmailBuilder:
    """Stand-in for the SDK's chainable email endpoint.

    Records the chain rather than asserting on it, so each test can pin only the
    part it is about. Mirrors the real builder in returning `self` from every step -
    including the mutability that `_send_via_lettermint` avoids sharing.
    """

    def __init__(self):
        self.calls: dict = {}
        self.sent = False

    def _record(self, key, value):
        self.calls[key] = value
        return self

    def from_(self, value):
        return self._record("from", value)

    def to(self, *values):
        return self._record("to", values)

    def subject(self, value):
        return self._record("subject", value)

    def text(self, value):
        return self._record("text", value)

    def html(self, value):
        return self._record("html", value)

    def route(self, value):
        return self._record("route", value)

    async def send(self):
        self.sent = True
        return {"message_id": "msg_123", "status": "queued"}


class _FakeAsyncLettermint:
    """Stand-in for AsyncLettermint. `instances` collects every client the code
    under test built, which is how the per-send-client rule gets asserted."""

    instances: list = []

    def __init__(self, api_token, *, base_url=None, timeout=None):
        self.api_token = api_token
        self.base_url = base_url
        self.timeout = timeout
        self.email = _FakeEmailBuilder()
        self.closed = False
        type(self).instances.append(self)

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        self.closed = True


@pytest.fixture
def lettermint(monkeypatch):
    """EMAIL_PROVIDER=lettermint with a fake SDK, minimally configured."""
    _FakeAsyncLettermint.instances = []
    monkeypatch.setattr(settings, "EMAIL_PROVIDER", "lettermint")
    monkeypatch.setattr(settings, "LETTERMINT_API_TOKEN", "lm_token")
    monkeypatch.setattr(settings, "LETTERMINT_API_BASE_URL", None)
    monkeypatch.setattr(settings, "LETTERMINT_TIMEOUT_SECONDS", 30.0)
    monkeypatch.setattr(settings, "LETTERMINT_ROUTE", None)
    monkeypatch.setattr(settings, "LETTERMINT_FROM_EMAIL", None)
    monkeypatch.setattr(settings, "LETTERMINT_FROM_NAME", None)
    monkeypatch.setattr(email, "AsyncLettermint", _FakeAsyncLettermint)
    return _FakeAsyncLettermint


class TestSendEmailViaLettermint:
    async def test_sends_with_expected_fields(self, lettermint, monkeypatch):
        monkeypatch.setattr(settings, "LETTERMINT_FROM_EMAIL", "hi@lettermint.test")
        monkeypatch.setattr(settings, "LETTERMINT_FROM_NAME", "Poulse Kora")

        await email.send_email("to@example.com", "hi", "body text")

        client = lettermint.instances[0]
        assert client.api_token == "lm_token"
        assert client.email.calls == {
            "from": "Poulse Kora <hi@lettermint.test>",
            "to": ("to@example.com",),
            "subject": "hi",
            "text": "body text",
        }
        assert client.email.sent is True

    async def test_html_is_sent_alongside_the_text_part(self, lettermint):
        await email.send_email("to@example.com", "hi", "body text", "<p>body</p>")

        calls = lettermint.instances[0].email.calls
        assert calls["text"] == "body text"
        assert calls["html"] == "<p>body</p>"

    async def test_html_is_omitted_when_not_given(self, lettermint):
        """`.html(None)` would send a null html key rather than no key."""
        await email.send_email("to@example.com", "hi", "body text")

        assert "html" not in lettermint.instances[0].email.calls

    async def test_client_is_closed(self, lettermint):
        """The SDK client owns an httpx pool; a send that leaked one per email
        would run the process out of sockets long before anyone noticed."""
        await email.send_email("to@example.com", "hi", "body")

        assert lettermint.instances[0].closed is True

    async def test_each_send_gets_its_own_client(self, lettermint):
        """The SDK caches one mutable builder per client, so two sends sharing a
        client could interleave into one message addressed to the wrong user."""
        await email.send_email("first@example.com", "one", "body")
        await email.send_email("second@example.com", "two", "body")

        assert len(lettermint.instances) == 2
        assert lettermint.instances[0].email is not lettermint.instances[1].email

    async def test_from_falls_back_to_smtp_identity(self, lettermint, monkeypatch):
        monkeypatch.setattr(settings, "SMTP_FROM_EMAIL", "no-reply@poulse.com")
        monkeypatch.setattr(settings, "SMTP_FROM_NAME", "Poulse Kora")

        await email.send_email("to@example.com", "hi", "body")

        from_header = lettermint.instances[0].email.calls["from"]
        assert from_header == "Poulse Kora <no-reply@poulse.com>"

    async def test_route_is_omitted_when_unset(self, lettermint):
        """Unset must mean the account's default route, not a null route."""
        await email.send_email("to@example.com", "hi", "body")

        assert "route" not in lettermint.instances[0].email.calls

    async def test_route_is_passed_when_set(self, lettermint, monkeypatch):
        monkeypatch.setattr(settings, "LETTERMINT_ROUTE", "transactional")

        await email.send_email("to@example.com", "hi", "body")

        assert lettermint.instances[0].email.calls["route"] == "transactional"

    async def test_base_url_and_timeout_are_forwarded(self, lettermint, monkeypatch):
        monkeypatch.setattr(
            settings, "LETTERMINT_API_BASE_URL", "https://mock.lettermint.test/v1"
        )
        monkeypatch.setattr(settings, "LETTERMINT_TIMEOUT_SECONDS", 5.0)

        await email.send_email("to@example.com", "hi", "body")

        client = lettermint.instances[0]
        assert client.base_url == "https://mock.lettermint.test/v1"
        assert client.timeout == 5.0

    async def test_smtp_is_not_touched(self, lettermint, monkeypatch):
        """Unset SMTP_HOST is the *SMTP* connector's log-instead-of-send fallback.
        It must not leak across connectors and quietly log a verification code."""
        monkeypatch.setattr(settings, "SMTP_HOST", None)
        smtp_send = AsyncMock()
        monkeypatch.setattr(email.aiosmtplib, "send", smtp_send)

        await email.send_email("to@example.com", "hi", "body")

        smtp_send.assert_not_called()
        assert lettermint.instances[0].email.sent is True

    async def test_send_failure_propagates(self, lettermint, monkeypatch):
        """Callers decide whether a failed send is fatal (a resend answers 502, a
        registration only logs), so this layer must not swallow anything."""

        async def boom(self):
            raise RuntimeError("lettermint down")

        monkeypatch.setattr(_FakeEmailBuilder, "send", boom)

        with pytest.raises(RuntimeError):
            await email.send_email("to@example.com", "hi", "body")

    async def test_rejection_logs_the_api_reason(self, lettermint, monkeypatch, caplog):
        """A rejected send must be diagnosable from the log. The SDK's exception
        stringifies to "Validation error: ValidationError" and the caller's
        log.exception writes a traceback naming no cause, so without this the only
        way to learn that a sending domain is unverified is to reproduce it."""

        async def rejected(self):
            raise HttpRequestError(
                "Validation error: ValidationError",
                422,
                {"message": "The domain 'unverified.example' is not verified."},
            )

        monkeypatch.setattr(_FakeEmailBuilder, "send", rejected)

        with caplog.at_level(logging.ERROR), pytest.raises(HttpRequestError):
            await email.send_email("to@example.com", "hi", "body")

        record = next(r for r in caplog.records if r.msg == "email.lettermint_rejected")
        assert record.fields["status"] == 422
        assert "is not verified" in record.fields["detail"]

    async def test_rejection_does_not_log_the_recipient(
        self, lettermint, monkeypatch, caplog
    ):
        """Lettermint quotes values in some of its errors, so one about the `to`
        field could echo an address into the log - which is the one thing
        app/core/logger.py rules out."""

        async def rejected(self):
            raise HttpRequestError(
                "Validation error: ValidationError",
                422,
                {"errors": {"to": ["The address someone@example.com is blocked."]}},
            )

        monkeypatch.setattr(_FakeEmailBuilder, "send", rejected)

        with caplog.at_level(logging.ERROR), pytest.raises(HttpRequestError):
            await email.send_email("someone@example.com", "hi", "body")

        record = next(r for r in caplog.records if r.msg == "email.lettermint_rejected")
        detail = record.fields["detail"]
        assert "someone@example.com" not in detail
        assert "<recipient>" in detail
        assert "is blocked" in detail


class TestSmtpRemainsTheDefault:
    def test_smtp_is_the_declared_default(self):
        """Read off the field rather than off `settings`, which reflects whatever
        .env the developer running the suite happens to have."""
        assert type(settings).model_fields["EMAIL_PROVIDER"].default == "smtp"

    async def test_smtp_provider_uses_smtp(self, monkeypatch):
        """The setting is new; an environment that says nothing must keep sending
        exactly the way it did before it existed."""
        monkeypatch.setattr(settings, "EMAIL_PROVIDER", "smtp")
        monkeypatch.setattr(settings, "SMTP_HOST", "smtp.example.com")
        send_mock = AsyncMock()
        monkeypatch.setattr(email.aiosmtplib, "send", send_mock)
        monkeypatch.setattr(
            email, "AsyncLettermint", AsyncMock(side_effect=AssertionError)
        )

        await email.send_email("to@example.com", "hi", "body")

        send_mock.assert_awaited_once()


class TestEmailProviderConfigValidation:
    """The Lettermint connector has no log-instead-of-send fallback on purpose, so
    selecting it without a token has to fail at startup rather than at 3am."""

    def _settings(self, **overrides):
        return type(settings)(**{**settings.model_dump(mode="json"), **overrides})

    def test_lettermint_without_token_is_rejected(self):
        with pytest.raises(ValidationError, match="LETTERMINT_API_TOKEN"):
            self._settings(EMAIL_PROVIDER="lettermint", LETTERMINT_API_TOKEN=None)

    def test_lettermint_with_token_is_accepted(self):
        loaded = self._settings(
            EMAIL_PROVIDER="lettermint", LETTERMINT_API_TOKEN="lm_token"
        )
        assert loaded.EMAIL_PROVIDER == "lettermint"

    def test_smtp_without_lettermint_token_is_fine(self):
        loaded = self._settings(EMAIL_PROVIDER="smtp", LETTERMINT_API_TOKEN=None)
        assert loaded.EMAIL_PROVIDER == "smtp"

    def test_unknown_provider_is_rejected(self):
        with pytest.raises(ValidationError):
            self._settings(EMAIL_PROVIDER="sendgrid")

    def test_rejection_does_not_leak_other_settings(self):
        """The crash exists to keep secrets out of logs, so it must not put one
        there on its way out. Pydantic quotes the validated input in the error;
        scoping the check to this field makes that input the missing token itself
        rather than the whole settings dict, SECRET_KEY included."""
        with pytest.raises(ValidationError) as raised:
            self._settings(EMAIL_PROVIDER="lettermint", LETTERMINT_API_TOKEN=None)

        assert settings.SECRET_KEY not in str(raised.value)
