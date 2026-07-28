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
from app.deps.db import CurrentAsyncSession
from app.deps.redis import CurrentRedis
from app.deps.users import CurrentUser
from app.schemas.email_verification import (
    EmailVerificationConfirm,
    EmailVerificationStatus,
)

router = APIRouter(prefix="/auth/email-verification", tags=["auth"])


@router.post("/resend", response_model=EmailVerificationStatus)
async def resend_email_verification_code(user: CurrentUser, redis: CurrentRedis):
    if user.is_verified:
        return EmailVerificationStatus(is_verified=True)

    remaining = await ev.resend_cooldown_remaining(redis, str(user.id))
    if remaining > 0:
        exc = api_error(429, "resend_cooldown", retry_after=remaining)
        exc.headers = {"Retry-After": str(remaining)}
        raise exc

    code = await ev.issue_code(redis, str(user.id))
    await ev.start_resend_cooldown(redis, str(user.id))
    subject, body = ev.email_content(code)
    await send_email(user.email, subject, body)
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
    if result == ev.VerifyResult.TOO_MANY_ATTEMPTS:
        raise api_error(429, "too_many_verification_attempts")
    if result == ev.VerifyResult.EXPIRED:
        raise api_error(400, "verification_code_expired")
    if result == ev.VerifyResult.WRONG_CODE:
        raise api_error(400, "invalid_verification_code", attempts_remaining=remaining)

    user.is_verified = True
    await session.commit()
    return EmailVerificationStatus(is_verified=True)
