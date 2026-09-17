"""Route dependencies enforcing the per-user budgets (see app/core/rate_limit.py).

Attach one to a route with `dependencies=[Depends(limit_interactions)]` rather than
as a handler argument — nothing in the handler needs its result.

`limit_feedback` is the odd one out: its route works signed out, so it keys on the
client IP when there is no user id. Everything else here keys on the user alone.

It runs *before* the handler body, so an interaction that goes on to fail validation
(unknown channel, post no longer in the queue) still spends a slot. That is
deliberate: hammering the API with invalid ids is the same flood, and the check must
be cheap enough to sit in front of the work rather than behind it.
"""

import math
from typing import Annotated

from fastapi import Depends, Request

from app.core.config import settings
from app.core.errors import api_error
from app.core.logger import get_logger
from app.core.rate_limit import consume
from app.deps.redis import CurrentRedis
from app.deps.users import CurrentUser, CurrentVerifiedUser, OptionalUser

log = get_logger(__name__)


def _log_rejection(scope: str, identity: str, retry_after: int, limit: int) -> None:
    """One line for every throttled request, at WARNING.

    WARNING rather than INFO because a limit being hit is by definition unusual:
    the budgets are set well above what the UI can produce by hand, so a burst
    means either a client bug (a retry loop) or someone driving the API directly.
    The identity is logged because it is the only thing that tells those apart -
    for signed-out feedback it is an `ip:` key, which is exactly when it matters.
    """
    log.warning(
        "rate_limit.exceeded",
        scope=scope,
        identity=identity,
        retry_after=retry_after,
        limit=limit,
    )


# All feed writes (create post, forward, drop) share this one budget, so a user
# cannot dodge it by alternating between endpoints.
INTERACTION_SCOPE = "interact"

# Separate from INTERACTION_SCOPE: change-password checks a guessable secret (the
# current password), so it needs its own budget rather than sharing one that a
# burst of ordinary posting/reviewing would also draw down.
PASSWORD_CHANGE_SCOPE = "change_password"

# Feedback submission, which is reachable signed out - so this is the one scope
# whose identity may be a client IP rather than a user id. See `limit_feedback`.
FEEDBACK_SCOPE = "feedback"

# The account data export. Its own budget because what it protects is bandwidth
# and bucket reads rather than the feed economy: a download must not be able to
# spend somebody's ability to post, and posting must not be able to spend their
# ability to exercise a right.
ACCOUNT_EXPORT_SCOPE = "account_export"


async def limit_interactions(user: CurrentVerifiedUser, redis: CurrentRedis) -> None:
    """Spend one interaction slot, or raise 429 with the wait in seconds.

    Superusers are exempt, matching how they bypass the token economy and the review
    gate. Setting `INTERACTION_RATE_LIMIT` to 0 disables the limit outright.
    """
    if settings.INTERACTION_RATE_LIMIT <= 0 or user.is_superuser:
        return

    retry_ms = await consume(
        redis,
        INTERACTION_SCOPE,
        str(user.id),
        settings.INTERACTION_RATE_LIMIT,
        settings.INTERACTION_RATE_WINDOW_SECONDS,
    )
    if retry_ms <= 0:
        return

    # Round up, and never advertise 0 seconds — a client obeying it would retry
    # immediately and be rejected again.
    retry_after = max(1, math.ceil(retry_ms / 1000))
    _log_rejection(
        INTERACTION_SCOPE, str(user.id), retry_after, settings.INTERACTION_RATE_LIMIT
    )
    exc = api_error(
        429,
        "rate_limited",
        retry_after=retry_after,
        limit=settings.INTERACTION_RATE_LIMIT,
        window_seconds=settings.INTERACTION_RATE_WINDOW_SECONDS,
    )
    # Standard header alongside the structured body, so non-app clients (and any
    # proxy in front of us) see the backoff too. `setup_exception_handlers` passes
    # `exc.headers` through to the response.
    exc.headers = {"Retry-After": str(retry_after)}
    raise exc


InteractionRateLimit = Annotated[None, Depends(limit_interactions)]


async def limit_password_change(user: CurrentUser, redis: CurrentRedis) -> None:
    """Spend one change-password attempt, or raise 429 with the wait in seconds.

    `CurrentUser`, not `CurrentVerifiedUser`: securing an account (changing its
    password) must work regardless of email-verification status, same reasoning
    as the email-verification routes themselves. Superusers are exempt, matching
    `limit_interactions`. Setting `PASSWORD_CHANGE_RATE_LIMIT` to 0 disables it.
    """
    if settings.PASSWORD_CHANGE_RATE_LIMIT <= 0 or user.is_superuser:
        return

    retry_ms = await consume(
        redis,
        PASSWORD_CHANGE_SCOPE,
        str(user.id),
        settings.PASSWORD_CHANGE_RATE_LIMIT,
        settings.PASSWORD_CHANGE_RATE_WINDOW_SECONDS,
    )
    if retry_ms <= 0:
        return

    retry_after = max(1, math.ceil(retry_ms / 1000))
    _log_rejection(
        PASSWORD_CHANGE_SCOPE,
        str(user.id),
        retry_after,
        settings.PASSWORD_CHANGE_RATE_LIMIT,
    )
    exc = api_error(
        429,
        "rate_limited",
        retry_after=retry_after,
        limit=settings.PASSWORD_CHANGE_RATE_LIMIT,
        window_seconds=settings.PASSWORD_CHANGE_RATE_WINDOW_SECONDS,
    )
    exc.headers = {"Retry-After": str(retry_after)}
    raise exc


async def limit_feedback(
    request: Request, user: OptionalUser, redis: CurrentRedis
) -> None:
    """Spend one feedback-submission slot, or raise 429 with the wait in seconds.

    Its own budget, and the only one here that has to work for a caller with no
    account: the feedback form is reachable from the login screen (see
    app/api/feedback.py), so there is frequently no user id to key on. Signed in it
    keys on the user id like every other limiter; signed out it falls back to the
    client IP.

    Two things about that fallback are deliberate:

    - **`request.client.host`, not `X-Forwarded-For`.** A forwarded-for header is
      client-supplied unless a trusted proxy is known to overwrite it, and trusting
      it here would turn the limit into an opt-out (send a fresh fake IP per
      request). The cost of not trusting it is the opposite error: behind a proxy
      every submission looks like one host and shares one budget, which throttles
      more than intended rather than less. If a proxy in front of this is ever
      trusted, that is the place to fix it - not here.
    - **A signed-in user is keyed by id, so signing out is not a way around a spent
      budget** - and, conversely, one busy office network cannot exhaust an
      identified user's own allowance.

    Superusers are exempt, matching the other limiters. Setting `FEEDBACK_RATE_LIMIT`
    to 0 disables it.
    """
    if settings.FEEDBACK_RATE_LIMIT <= 0 or (user is not None and user.is_superuser):
        return

    if user is not None:
        identity = str(user.id)
    else:
        identity = f"ip:{request.client.host if request.client else 'unknown'}"

    retry_ms = await consume(
        redis,
        FEEDBACK_SCOPE,
        identity,
        settings.FEEDBACK_RATE_LIMIT,
        settings.FEEDBACK_RATE_WINDOW_SECONDS,
    )
    if retry_ms <= 0:
        return

    retry_after = max(1, math.ceil(retry_ms / 1000))
    _log_rejection(FEEDBACK_SCOPE, identity, retry_after, settings.FEEDBACK_RATE_LIMIT)
    exc = api_error(
        429,
        "rate_limited",
        retry_after=retry_after,
        limit=settings.FEEDBACK_RATE_LIMIT,
        window_seconds=settings.FEEDBACK_RATE_WINDOW_SECONDS,
    )
    exc.headers = {"Retry-After": str(retry_after)}
    raise exc


async def limit_account_export(user: CurrentUser, redis: CurrentRedis) -> None:
    """Spend one export slot, or raise 429 with the wait in seconds.

    `CurrentUser`, not `CurrentVerifiedUser`, for the same reason `DELETE
    /users/me` uses it: the right of access does not depend on having got round
    to clicking a link in an email. Superusers are exempt, matching every other
    limiter here. Setting `ACCOUNT_EXPORT_RATE_LIMIT` to 0 disables it.
    """
    if settings.ACCOUNT_EXPORT_RATE_LIMIT <= 0 or user.is_superuser:
        return

    retry_ms = await consume(
        redis,
        ACCOUNT_EXPORT_SCOPE,
        str(user.id),
        settings.ACCOUNT_EXPORT_RATE_LIMIT,
        settings.ACCOUNT_EXPORT_RATE_WINDOW_SECONDS,
    )
    if retry_ms <= 0:
        return

    retry_after = max(1, math.ceil(retry_ms / 1000))
    _log_rejection(
        ACCOUNT_EXPORT_SCOPE,
        str(user.id),
        retry_after,
        settings.ACCOUNT_EXPORT_RATE_LIMIT,
    )
    exc = api_error(
        429,
        "rate_limited",
        retry_after=retry_after,
        limit=settings.ACCOUNT_EXPORT_RATE_LIMIT,
        window_seconds=settings.ACCOUNT_EXPORT_RATE_WINDOW_SECONDS,
    )
    exc.headers = {"Retry-After": str(retry_after)}
    raise exc
