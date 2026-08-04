"""Route dependency enforcing the per-user interaction budget.

Attach it to a route with `dependencies=[Depends(limit_interactions)]` rather than
as a handler argument — nothing in the handler needs its result.

It runs *before* the handler body, so an interaction that goes on to fail validation
(unknown channel, post no longer in the queue) still spends a slot. That is
deliberate: hammering the API with invalid ids is the same flood, and the check must
be cheap enough to sit in front of the work rather than behind it.
"""

import math
from typing import Annotated

from fastapi import Depends

from app.core.config import settings
from app.core.errors import api_error
from app.core.rate_limit import consume
from app.deps.redis import CurrentRedis
from app.deps.users import CurrentUser, CurrentVerifiedUser

# All feed writes (create post, forward, drop) share this one budget, so a user
# cannot dodge it by alternating between endpoints.
INTERACTION_SCOPE = "interact"

# Separate from INTERACTION_SCOPE: change-password checks a guessable secret (the
# current password), so it needs its own budget rather than sharing one that a
# burst of ordinary posting/reviewing would also draw down.
PASSWORD_CHANGE_SCOPE = "change_password"


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
    exc = api_error(
        429,
        "rate_limited",
        retry_after=retry_after,
        limit=settings.PASSWORD_CHANGE_RATE_LIMIT,
        window_seconds=settings.PASSWORD_CHANGE_RATE_WINDOW_SECONDS,
    )
    exc.headers = {"Retry-After": str(retry_after)}
    raise exc
