"""Outbound transactional email via plain SMTP.

No provider SDK: any SMTP relay (Gmail, AWS SES, Mailgun, Postmark, ...) works
unchanged by just setting SMTP_* in .env. See Settings.SMTP_HOST for the local
dev/test fallback (logs instead of sending).
"""

import logging
from email.message import EmailMessage

import aiosmtplib

from app.core.config import settings

logger = logging.getLogger(__name__)


async def send_email(to: str, subject: str, body: str) -> None:
    if not settings.SMTP_HOST:
        logger.info(
            "SMTP not configured; not sending email to %s: %r\n%s", to, subject, body
        )
        return

    message = EmailMessage()
    message["From"] = f"{settings.SMTP_FROM_NAME} <{settings.SMTP_FROM_EMAIL}>"
    message["To"] = to
    message["Subject"] = subject
    message.set_content(body)

    await aiosmtplib.send(
        message,
        hostname=settings.SMTP_HOST,
        port=settings.SMTP_PORT,
        username=settings.SMTP_USERNAME or None,
        password=settings.SMTP_PASSWORD or None,
        start_tls=settings.SMTP_USE_TLS,
    )
