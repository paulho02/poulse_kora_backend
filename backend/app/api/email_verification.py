"""Confirm/resend endpoints for the short-code email verification flow (see
app.core.email_verification for why this isn't fastapi-users' built-in verify
router). Both routes run on `CurrentUser` (active only) rather than
`CurrentVerifiedUser` - obviously, since their whole purpose is to work *before*
the account is verified.
"""

from fastapi import APIRouter

from app.core import email_verification as ev
from app.core.email import send_email
from app.core.errors import api_error
from app.core.logger import get_logger
from app.deps.db import CurrentAsyncSession
from app.deps.locale import CurrentLocale
from app.deps.redis import CurrentRedis
from app.deps.users import CurrentUser
from app.schemas.email_verification import (
    EmailVerificationConfirm,
    EmailVerificationStatus,
)

log = get_logger(__name__)

router = APIRouter(prefix="/auth/email-verification", tags=["auth"])


@router.post("/resend", response_model=EmailVerificationStatus)
async def resend_email_verification_code(
    user: CurrentUser, redis: CurrentRedis, locale: CurrentLocale
):
    if user.is_verified:
        return EmailVerificationStatus(is_verified=True)

    remaining = await ev.resend_cooldown_remaining(redis, str(user.id))
    if remaining > 0:
        exc = api_error(429, "resend_cooldown", retry_after=remaining)
        exc.headers = {"Retry-After": str(remaining)}
        raise exc

    code = await ev.issue_code(redis, str(user.id))
    subject, body, html = ev.email_content(code, locale)
    try:
        await send_email(user.email, subject, body, html)
    except Exception:
        # Don't start the cooldown for a send that never went out - otherwise a
        # transient SMTP hiccup locks the user out of a real retry for a full
        # cooldown window with nothing ever delivered.
        log.exception(
            "email.verification_send_failed", user_id=str(user.id), on="resend"
        )
        raise api_error(502, "email_send_failed") from None
    await ev.start_resend_cooldown(redis, str(user.id))
    log.info("email.verification_code_sent", user_id=str(user.id), on="resend")
    return EmailVerificationStatus(is_verified=False)


@router.post("/confirm", response_model=EmailVerificationStatus)
async def confirm_email_verification(
    body: EmailVerificationConfirm,
    user: CurrentUser,
    session: CurrentAsyncSession,
    redis: CurrentRedis,
):
    if user.is_verified:
        return EmailVerificationStatus(is_verified=True)

    result, remaining = await ev.check_code(redis, str(user.id), body.code.strip())
    if result != ev.VerifyResult.OK:
        # One line for all three failures, with the reason as a field: a code
        # never arriving (expired), being mistyped (wrong_code) and being guessed
        # at (too_many_attempts) look identical to the user and completely
        # different from here.
        # `VerifyResult`'s members are plain strings, so the reason lands on the
        # line as "wrong_code"/"expired"/"too_many_attempts" as-is.
        log.info(
            "email.verification_failed",
            user_id=str(user.id),
            reason=result,
            attempts_remaining=remaining,
        )
    if result == ev.VerifyResult.TOO_MANY_ATTEMPTS:
        raise api_error(429, "too_many_verification_attempts")
    if result == ev.VerifyResult.EXPIRED:
        raise api_error(400, "verification_code_expired")
    if result == ev.VerifyResult.WRONG_CODE:
        raise api_error(400, "invalid_verification_code", attempts_remaining=remaining)

    user.is_verified = True
    await session.commit()
    log.info("user.email_verified", user_id=str(user.id))
    return EmailVerificationStatus(is_verified=True)
