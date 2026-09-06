"""Outbound transactional email via plain SMTP.

No provider SDK: any SMTP relay (Gmail, AWS SES, Mailgun, Postmark, ...) works
unchanged by just setting SMTP_* in .env. See Settings.SMTP_HOST for the local
dev/test fallback (logs instead of sending).
"""

from email.message import EmailMessage

import aiosmtplib

from app.core.config import settings
from app.core.logger import get_logger

log = get_logger(__name__)


async def send_email(to: str, subject: str, body: str) -> None:
    if not settings.SMTP_HOST:
        # The one place an address and a body are logged, and deliberately
        # so: with no relay configured this *is* the delivery mechanism (a
        # dev reads the verification code out of the log). It cannot fire in
        # an environment that sends real mail, because having a relay is what
        # makes it not fire.
        log.info("email.not_sent_smtp_unconfigured", to=to, subject=subject, body=body)
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
    # No address on the line: `subject` says which mail went out and the caller
    # logs the user id. Failures are left to the callers, which are the ones that
    # know whether a failure is fatal to what the user asked for.
    log.info("email.sent", subject=subject)
