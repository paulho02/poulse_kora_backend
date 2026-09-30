"""Short numeric password-reset codes, stored in Redis - the same shape as
app.core.email_verification, and for the same reason: not fastapi-users' own
`forgot_password`/`reset_password`, which mint/verify a long-lived JWT meant to
be opened as a link, unsuited to an in-app "enter the 6-digit code" field.

Deliberately no resend-cooldown state here, unlike email_verification.py: this
flow's abuse control is the *request-time* rate limit
(`app.deps.rate_limit.limit_forgot_password`), keyed on the submitted email
whether or not it belongs to an account. A per-user cooldown would have to be
skipped for a nonexistent or Google-linked account (nothing to attach it to),
and skipping it is exactly the differential response that turns "forgot
password" into an account-enumeration oracle - see app/api/password_reset.py.
"""

import secrets

from redis.asyncio import Redis

from app.core import email_templates as templates
from app.core.config import settings

_CODE_KEY = "pwd_reset:code:{identity}"
_ATTEMPTS_KEY = "pwd_reset:attempts:{identity}"


class ResetResult:
    OK = "ok"
    WRONG_CODE = "wrong_code"
    EXPIRED = "expired"
    TOO_MANY_ATTEMPTS = "too_many_attempts"


def _generate_code() -> str:
    upper = 10**settings.PASSWORD_RESET_CODE_LENGTH
    return str(secrets.randbelow(upper)).zfill(settings.PASSWORD_RESET_CODE_LENGTH)


def email_content(code: str, locale: str) -> tuple[str, str, str]:
    """(subject, text, html) for the reset-code email - a thin pass-through to
    app/core/email_templates.py, kept here so the route only imports one name
    from the module that owns the code itself."""
    return templates.password_reset_email(code, locale)


async def issue_code(redis: Redis, identity: str) -> str:
    """Generate a fresh code for `identity`, replacing any previous one and
    resetting attempts. `identity` is a real user id when a code is actually
    being issued to an account; see `check_code` for the pseudo-id case."""
    code = _generate_code()
    async with redis.pipeline(transaction=True) as pipe:
        pipe.set(
            _CODE_KEY.format(identity=identity),
            code,
            ex=settings.PASSWORD_RESET_CODE_TTL_SECONDS,
        )
        pipe.delete(_ATTEMPTS_KEY.format(identity=identity))
        await pipe.execute()
    return code


async def check_code(redis: Redis, identity: str, submitted: str) -> tuple[str, int]:
    """Returns (ResetResult, attempts_remaining_after_this_try).

    `identity` is either a real user id a code was actually issued to, or an
    opaque pseudo-id (see `app.core.rate_limit.opaque_identity`) standing in
    for one that wasn't - a nonexistent email, a Google-linked account, an
    inactive one. Every pseudo-id has no stored code, so that path always
    resolves to EXPIRED - the same answer a real account gets before ever
    requesting a reset. The two are indistinguishable from the response alone,
    which is the point: the confirm route must not tell a caller which of
    "wrong code" and "no such account" happened.
    """
    attempts_key = _ATTEMPTS_KEY.format(identity=identity)
    code_key = _CODE_KEY.format(identity=identity)

    attempts = await redis.incr(attempts_key)
    if attempts == 1:
        # First attempt against this code: tie the counter's lifetime to the
        # code's remaining TTL, so it can't outlive the code and block a future
        # request. For a pseudo-id (no code ever set) this falls back to the
        # nominal TTL, which still expires the counter on its own.
        ttl = await redis.ttl(code_key)
        fallback_ttl = settings.PASSWORD_RESET_CODE_TTL_SECONDS
        await redis.expire(attempts_key, ttl if ttl > 0 else fallback_ttl)

    remaining = max(0, settings.PASSWORD_RESET_MAX_ATTEMPTS - attempts)
    if attempts > settings.PASSWORD_RESET_MAX_ATTEMPTS:
        return ResetResult.TOO_MANY_ATTEMPTS, 0

    stored = await redis.get(code_key)
    if stored is None:
        return ResetResult.EXPIRED, remaining
    if not secrets.compare_digest(stored, submitted):
        return ResetResult.WRONG_CODE, remaining

    return ResetResult.OK, remaining


async def consume_code(redis: Redis, identity: str) -> None:
    """Invalidate `identity`'s code once it has actually been used.

    Deliberately not folded into `check_code`'s OK branch: the route calls this
    only after the new password is written, not merely once the code checks
    out - a code that checks out but is followed by a rejected weak new
    password must still be usable on an immediate retry, rather than forcing
    the caller back through `forgot_password` for a fresh one over what was
    only ever a password-policy mistake.
    """
    await redis.delete(
        _CODE_KEY.format(identity=identity), _ATTEMPTS_KEY.format(identity=identity)
    )
