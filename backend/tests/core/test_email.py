"""app.core.email.send_email: the SMTP-configured path is only reachable when
SMTP_HOST is set, which it never is in local dev/test config (see CLAUDE.md) - so
it never gets exercised just by running the rest of the suite. Mocked here since
hitting a real SMTP relay in tests isn't practical."""

from unittest.mock import AsyncMock

import pytest

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
        monkeypatch.setattr(settings, "SMTP_FROM_EMAIL", "no-reply@poulsekora.app")
        monkeypatch.setattr(settings, "SMTP_FROM_NAME", "Poulse Kora")

        send_mock = AsyncMock()
        monkeypatch.setattr(email.aiosmtplib, "send", send_mock)

        await email.send_email("to@example.com", "hi", "body text")

        send_mock.assert_awaited_once()
        message = send_mock.await_args.args[0]
        assert message["To"] == "to@example.com"
        assert message["Subject"] == "hi"
        assert message["From"] == "Poulse Kora <no-reply@poulsekora.app>"
        assert message.get_content().strip() == "body text"

        kwargs = send_mock.await_args.kwargs
        assert kwargs["hostname"] == "smtp.example.com"
        assert kwargs["port"] == 2525
        assert kwargs["username"] == "user"
        assert kwargs["password"] == "pass"
        assert kwargs["start_tls"] is True

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
