"""Outbound transactional email, over one of two interchangeable connectors.

`send_email` is the only entry point the rest of the codebase knows about;
`settings.EMAIL_PROVIDER` picks what it hands the message to:

- **"smtp"** (default) - plain SMTP via `aiosmtplib`. No provider SDK, so any relay
  (Gmail, AWS SES, Mailgun, Postmark, ...) works unchanged by just setting SMTP_* in
  .env. See `Settings.SMTP_HOST` for the local dev/test fallback (logs instead of
  sending).
- **"lettermint"** - https://lettermint.co, an EU-hosted transactional provider,
  through its official SDK. Requires LETTERMINT_API_TOKEN, which config.py enforces
  at startup: this connector has no log-instead-of-send fallback, on purpose.

Both send the same plain-text message. Callers are what decides whether a failure is
fatal to the request, so nothing is swallowed here - a connector raising is the
contract, and every call site already wraps this in a try/except.
"""

import json
from email.message import EmailMessage

import aiosmtplib
from lettermint import AsyncLettermint
from lettermint.exceptions import HttpRequestError

from app.core.config import settings
from app.core.logger import get_logger

log = get_logger(__name__)


def delivery_configured() -> bool:
    """Whether the selected connector can actually put mail on the wire.

    Reported on the `app.started` line, which is the one place a deploy answers
    "why is nobody getting verification emails" without a reproduction. False here
    and REQUIRE_EMAIL_VERIFICATION true is the combination that strands users.
    Lettermint is always configured when selected - config.py refuses to start
    otherwise - so only SMTP can be selected-but-inert.
    """
    if settings.EMAIL_PROVIDER == "lettermint":
        return True
    return bool(settings.SMTP_HOST)


async def send_email(to: str, subject: str, body: str, html: str | None = None) -> None:
    """`body` is the plain-text version and is never optional.

    An HTML mail is sent as `multipart/alternative` with both parts, not as HTML
    alone: some recipients are configured to prefer text, a screen reader is
    happier with it, and an HTML-only alternative part is a spam signal in its
    own right. So `html` enriches the message; it never replaces it.
    """
    if settings.EMAIL_PROVIDER == "lettermint":
        await _send_via_lettermint(to, subject, body, html)
    else:
        await _send_via_smtp(to, subject, body, html)


async def _send_via_smtp(
    to: str, subject: str, body: str, html: str | None = None
) -> None:
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
    # `add_alternative` after `set_content` is what makes this
    # multipart/alternative with text first and HTML second - the order matters,
    # since a client picks the *last* part it can render.
    if html:
        message.add_alternative(html, subtype="html")

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
    log.info("email.sent", provider="smtp", subject=subject)


def _redact_recipient(response_body: object, to: str) -> str:
    """Lettermint's error payload, flattened to a string with the recipient taken
    back out.

    Most of these errors name a *field* rather than a value, but not all - the
    one for an unverified sending domain quotes it - so a rule about the `to`
    address could echo the address back. That would put an email address in the
    log, which app/core/logger.py rules out; the user id the caller logs is one
    join away from it for whoever is entitled to look.
    """
    try:
        rendered = json.dumps(response_body, sort_keys=True)
    except (TypeError, ValueError):
        rendered = repr(response_body)
    return rendered.replace(to, "<recipient>")


async def _send_via_lettermint(
    to: str, subject: str, body: str, html: str | None = None
) -> None:
    from_email = settings.LETTERMINT_FROM_EMAIL or settings.SMTP_FROM_EMAIL
    from_name = settings.LETTERMINT_FROM_NAME or settings.SMTP_FROM_NAME

    # A client per send, rather than one long-lived module-level client. The SDK's
    # builder is mutable and *cached* on the client (`instance.email` hands back the
    # same object every time), so two coroutines sharing one client would interleave
    # their .to()/.subject() calls and mail one user another user's verification
    # code. Building a fresh one costs a TLS handshake per message, which is nothing
    # at the volume this sends at (a code per registration or resend), and it makes
    # the hazard structurally impossible instead of a comment someone has to obey.
    async with AsyncLettermint(
        settings.LETTERMINT_API_TOKEN,
        base_url=settings.LETTERMINT_API_BASE_URL,
        timeout=settings.LETTERMINT_TIMEOUT_SECONDS,
    ) as client:
        message = (
            client.email.from_(f"{from_name} <{from_email}>")
            .to(to)
            .subject(subject)
            .text(body)
        )
        if html:
            message = message.html(html)
        # Left unset the account's default route applies; passing None would send
        # the key with a null value rather than omit it.
        if settings.LETTERMINT_ROUTE:
            message = message.route(settings.LETTERMINT_ROUTE)
        try:
            response = await message.send()
        except HttpRequestError as exc:
            # The SDK's exceptions stringify to nothing usable - a rejected send
            # raises `ValidationError: Validation error: ValidationError`, and the
            # caller's log.exception then writes a traceback that names no cause.
            # Lettermint's own field errors are on `.response_body`, and they are
            # specific ("The domain 'x' is not verified or does not belong to your
            # account"), so this is the difference between reading the log and
            # reproducing the failure by hand.
            log.error(
                "email.lettermint_rejected",
                status=exc.status_code,
                detail=_redact_recipient(exc.response_body, to),
            )
            raise

    # Same rule as the SMTP line - no address. `message_id` is Lettermint's own
    # handle for the message, which is what their dashboard is searched by, so a
    # "the code never arrived" report can be traced from our logs into theirs.
    log.info(
        "email.sent",
        provider="lettermint",
        subject=subject,
        message_id=response.get("message_id"),
        status=response.get("status"),
    )
