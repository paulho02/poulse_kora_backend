"""Short numeric email-verification codes, stored in Redis.

Deliberately not fastapi-users' built-in verify flow (`get_verify_router` /
`UserManager.verify`), which mails a long-lived JWT meant to be opened as a link -
unsuited to an in-app "enter the 6-digit code" field. `is_verified` is still the
exact same column fastapi-users defines; only *how* it gets flipped is custom.
Enforcement of the flag lives in `app.deps.users.CurrentVerifiedUser`, not here.
"""

import secrets

from redis.asyncio import Redis

from app.core.config import settings

_CODE_KEY = "email_verify:code:{user_id}"
_ATTEMPTS_KEY = "email_verify:attempts:{user_id}"
_COOLDOWN_KEY = "email_verify:cooldown:{user_id}"


class VerifyResult:
    OK = "ok"
    WRONG_CODE = "wrong_code"
    EXPIRED = "expired"
    TOO_MANY_ATTEMPTS = "too_many_attempts"


def _generate_code() -> str:
    upper = 10**settings.EMAIL_VERIFICATION_CODE_LENGTH
    return str(secrets.randbelow(upper)).zfill(settings.EMAIL_VERIFICATION_CODE_LENGTH)


def email_content(code: str) -> tuple[str, str]:
    """(subject, body) for the verification email - shared by the on-register send
    and the resend endpoint so the copy only lives in one place."""
    minutes = settings.EMAIL_VERIFICATION_CODE_TTL_SECONDS // 60
    subject = "Your Poulse Kora verification code"
    body = (
        f"Your verification code is {code}.\n\n"
        f"It expires in {minutes} minutes. If you didn't request this, you can "
        "ignore this email."
    )
    return subject, body


async def issue_code(redis: Redis, user_id: str) -> str:
    """Generate a fresh code, replacing any previous one and resetting attempts."""
    code = _generate_code()
    async with redis.pipeline(transaction=True) as pipe:
        pipe.set(
            _CODE_KEY.format(user_id=user_id),
            code,
            ex=settings.EMAIL_VERIFICATION_CODE_TTL_SECONDS,
        )
        pipe.delete(_ATTEMPTS_KEY.format(user_id=user_id))
        await pipe.execute()
    return code


async def start_resend_cooldown(redis: Redis, user_id: str) -> None:
    await redis.set(
        _COOLDOWN_KEY.format(user_id=user_id),
        "1",
        ex=settings.EMAIL_VERIFICATION_RESEND_COOLDOWN_SECONDS,
    )


async def resend_cooldown_remaining(redis: Redis, user_id: str) -> int:
    """Seconds left before another resend is allowed, or 0 if allowed now."""
    ttl = await redis.ttl(_COOLDOWN_KEY.format(user_id=user_id))
    return max(0, ttl)


async def check_code(redis: Redis, user_id: str, submitted: str) -> tuple[str, int]:
    """Returns (VerifyResult, attempts_remaining_after_this_try)."""
    attempts_key = _ATTEMPTS_KEY.format(user_id=user_id)
    code_key = _CODE_KEY.format(user_id=user_id)

    attempts = await redis.incr(attempts_key)
    if attempts == 1:
        # First attempt against this code: tie the counter's lifetime to the code's
        # remaining TTL, so it can't outlive the code and block a future resend.
        ttl = await redis.ttl(code_key)
        fallback_ttl = settings.EMAIL_VERIFICATION_CODE_TTL_SECONDS
        await redis.expire(attempts_key, ttl if ttl > 0 else fallback_ttl)

    remaining = max(0, settings.EMAIL_VERIFICATION_MAX_ATTEMPTS - attempts)
    if attempts > settings.EMAIL_VERIFICATION_MAX_ATTEMPTS:
        return VerifyResult.TOO_MANY_ATTEMPTS, 0

    stored = await redis.get(code_key)
    if stored is None:
        return VerifyResult.EXPIRED, remaining
    if not secrets.compare_digest(stored, submitted):
        return VerifyResult.WRONG_CODE, remaining

    await redis.delete(code_key, attempts_key)
    return VerifyResult.OK, remaining
