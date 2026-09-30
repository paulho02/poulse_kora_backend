"""Forgot/reset-password: a short code emailed to the address, not fastapi-users'
own `forgot_password`/`reset_password` (see app/core/password_reset.py for why -
same reason as app/api/email_verification.py). Both routes are reachable signed
out by definition, so both are rate-limited by app/deps/rate_limit.py's
`limit_forgot_password`/`limit_reset_password_confirm` rather than anything
keyed on a user - there is no session to key on.
"""

from fastapi import APIRouter, Depends, status
from fastapi_users.exceptions import InvalidPasswordException, UserNotExists

from app.core import password_reset as pr
from app.core.email import send_email
from app.core.email_templates import password_reset_google_notice_email
from app.core.errors import api_error
from app.core.logger import get_logger
from app.core.rate_limit import opaque_identity
from app.deps.db import CurrentAsyncSession
from app.deps.locale import CurrentLocale
from app.deps.rate_limit import limit_forgot_password, limit_reset_password_confirm
from app.deps.redis import CurrentRedis
from app.deps.users import UserManager, get_user_manager
from app.schemas.msg import Msg
from app.schemas.password_reset import ForgotPasswordRequest, ResetPasswordConfirm

log = get_logger(__name__)

router = APIRouter(prefix="/auth", tags=["auth"])

#: Returned by every branch of `forgot_password` - see that route for why the
#: body must never vary with whether `email` turned out to belong to anyone.
_GENERIC_RESPONSE = Msg(msg="ok")

#: Pseudo-identity prefix for `check_code` when no code could have been issued
#: (unknown email, inactive or Google-linked account) - see `reset_password_confirm`.
_NO_ACCOUNT_PREFIX = "pwd_reset_noacct"


@router.post(
    "/forgot-password",
    response_model=Msg,
    dependencies=[Depends(limit_forgot_password)],
)
async def forgot_password(
    body: ForgotPasswordRequest,
    redis: CurrentRedis,
    locale: CurrentLocale,
    user_manager: UserManager = Depends(get_user_manager),
):
    """Always answers 200 with the same body, whether or not `email` belongs to
    an active, password-based account - a 404 for an unknown address would
    make this the textbook account-enumeration oracle. The client shows a
    fixed "check your email" message regardless of which branch below ran.

    Abuse is bounded before this handler ever runs, by `limit_forgot_password`:
    one budget per caller address, one per submitted email - the latter is
    what stands between this and mail-bombing one inbox with reset codes from
    many hosts, and doubles as the flow's resend cooldown (see
    app.core.password_reset's module docstring for why there isn't a second,
    separate one here).
    """
    try:
        user = await user_manager.get_by_email(body.email)
    except UserNotExists:
        log.info("auth.password_reset_requested", reason="unknown_account")
        return _GENERIC_RESPONSE

    if not user.is_active:
        # Same non-disclosure as google_auth.py's own `is_active` check -
        # nothing about the account's state reaches the response.
        log.info(
            "auth.password_reset_requested", user_id=str(user.id), reason="inactive"
        )
        return _GENERIC_RESPONSE

    if user.oauth_accounts:
        # Linking overwrote hashed_password with a random value nobody holds
        # (see app/api/google_auth.py) - there is no password to reset, so no
        # code is issued. A notice mail still goes out: the one thing worth
        # telling this account's real owner is what to do instead, and sending
        # it costs nothing an attacker could use, since only the inbox's owner
        # ever reads it.
        subject, text, html = password_reset_google_notice_email(locale)
        try:
            await send_email(user.email, subject, text, html)
        except Exception:
            log.exception(
                "email.password_reset_send_failed",
                user_id=str(user.id),
                on="google_notice",
            )
        else:
            log.info(
                "auth.password_reset_requested",
                user_id=str(user.id),
                reason="google_account",
            )
        return _GENERIC_RESPONSE

    code = await pr.issue_code(redis, str(user.id))
    subject, text, html = pr.email_content(code, locale)
    try:
        await send_email(user.email, subject, text, html)
    except Exception:
        # Best-effort, same reasoning as on_after_register: the alternative is
        # a 500 from a route that must always look like it succeeded.
        log.exception("email.password_reset_send_failed", user_id=str(user.id))
    else:
        log.info("auth.password_reset_requested", user_id=str(user.id))
    return _GENERIC_RESPONSE


@router.post(
    "/reset-password/confirm",
    status_code=status.HTTP_204_NO_CONTENT,
    dependencies=[Depends(limit_reset_password_confirm)],
)
async def reset_password_confirm(
    body: ResetPasswordConfirm,
    session: CurrentAsyncSession,
    redis: CurrentRedis,
    user_manager: UserManager = Depends(get_user_manager),
):
    """Redeems a code from `forgot_password` for a new password.

    Never reveals *why* a code was rejected beyond "wrong or expired" - not
    "no such account", not "that account signs in with Google" - for the same
    enumeration reason `forgot_password` always answers the same way. Every
    path below calls `check_code` against *some* identity: a real user id when
    a code could actually have been issued, an opaque pseudo-id derived from
    the submitted email otherwise (see `app.core.rate_limit.opaque_identity`)
    - so the Redis round trip, and the response, look the same either way.
    """
    try:
        user = await user_manager.get_by_email(body.email)
    except UserNotExists:
        user = None

    eligible = user is not None and user.is_active and not user.oauth_accounts
    identity = (
        str(user.id)
        if eligible
        else opaque_identity(_NO_ACCOUNT_PREFIX, body.email)
    )

    result, remaining = await pr.check_code(redis, identity, body.code.strip())
    if result != pr.ResetResult.OK:
        log.info(
            "auth.password_reset_failed",
            reason=result,
            attempts_remaining=remaining,
            user_id=str(user.id) if eligible else None,
        )
        if result == pr.ResetResult.TOO_MANY_ATTEMPTS:
            raise api_error(429, "too_many_password_reset_attempts")
        raise api_error(
            400,
            "password_reset_invalid_or_expired_code",
            attempts_remaining=remaining,
        )

    # `check_code` only returns OK for a code this route itself issued, which
    # only ever happened for an eligible account - asserted rather than
    # trusted, so a future change to that guarantee fails loudly here instead
    # of silently resetting nobody's password.
    assert eligible and user is not None

    try:
        await user_manager.validate_password(body.new_password, user)
    except InvalidPasswordException as exc:
        # Deliberately before `consume_code`: a code that checks out but is
        # followed by a rejected weak password must still work on an
        # immediate retry with a stronger one, not force a whole new
        # forgot-password round trip over a password-policy mistake.
        raise api_error(
            400, "reset_password_invalid_password", reason=exc.reason
        ) from exc

    user.hashed_password = user_manager.password_helper.hash(body.new_password)
    await session.commit()
    await pr.consume_code(redis, identity)
    log.info("auth.password_reset", user_id=str(user.id))
