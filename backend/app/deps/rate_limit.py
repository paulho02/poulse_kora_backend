"""Route dependencies enforcing the per-user budgets (see app/core/rate_limit.py).

Attach one to a route with `dependencies=[Depends(limit_interactions)]` rather than
as a handler argument — nothing in the handler needs its result.

Three of these key on something other than a signed-in user, because their routes
work signed out: `limit_feedback` falls back to the caller's address, `limit_login`
spends one budget on the address and one on the account being tried, and
`limit_register` has only an address to go on. Everything else keys on the user.

It runs *before* the handler body, so an interaction that goes on to fail validation
(unknown channel, post no longer in the queue) still spends a slot. That is
deliberate: hammering the API with invalid ids is the same flood, and the check must
be cheap enough to sit in front of the work rather than behind it.
"""

from typing import Annotated

from fastapi import Depends, Request

from app.core.config import settings
from app.core.logger import running_on_railway
from app.core.rate_limit import enforce, opaque_identity
from app.core.request_logging import caller_address
from app.deps.redis import CurrentRedis
from app.deps.users import CurrentUser, CurrentVerifiedUser, OptionalUser

# All feed writes (create post, forward, drop) share this one budget, so a user
# cannot dodge it by alternating between endpoints.
INTERACTION_SCOPE = "interact"

# Separate from INTERACTION_SCOPE: change-password checks a guessable secret (the
# current password), so it needs its own budget rather than sharing one that a
# burst of ordinary posting/reviewing would also draw down.
PASSWORD_CHANGE_SCOPE = "change_password"

# Feedback submission, which is reachable signed out - so this is one of the scopes
# whose identity may be a client address rather than a user id. See `limit_feedback`.
FEEDBACK_SCOPE = "feedback"

# The account data export. Its own budget because what it protects is bandwidth
# and bucket reads rather than the feed economy: a download must not be able to
# spend somebody's ability to post, and posting must not be able to spend their
# ability to exercise a right.
ACCOUNT_EXPORT_SCOPE = "account_export"

# Login attempts: one budget per caller address, one per account tried. See
# `limit_login` for why both.
LOGIN_SCOPE = "login"

# Registrations, per caller address.
REGISTER_SCOPE = "register"

# Where this process runs cannot change while it runs (same resolution the
# request logger makes once per middleware instance).
_BEHIND_PROXY = running_on_railway()


def client_identity(request: Request) -> str:
    """The budget identity for a caller with no account: `ip:<address>`.

    Derived by `caller_address`, the same function the request log uses, rather
    than read off `request.client`: behind Railway's edge the socket peer is the
    proxy for every request, so keying on it gave every signed-out caller on the
    deployment one shared budget - five feedback submissions from anyone locked
    the "I can't sign in" form for everybody for an hour. Trusting the *leftmost*
    `X-Forwarded-For` entry instead would be the opposite failure, a budget the
    caller resets by editing a header; `caller_address` takes the rightmost entry,
    the one our own trusted hop wrote, and only when running behind that hop.
    """
    return f"ip:{caller_address(request.scope, _BEHIND_PROXY) or 'unknown'}"


async def limit_interactions(user: CurrentVerifiedUser, redis: CurrentRedis) -> None:
    """Spend one interaction slot, or raise 429 with the wait in seconds.

    Superusers are exempt, matching how they bypass the token economy and the review
    gate. Setting `INTERACTION_RATE_LIMIT` to 0 disables the limit outright.
    """
    if user.is_superuser:
        return
    await enforce(
        redis,
        INTERACTION_SCOPE,
        str(user.id),
        settings.INTERACTION_RATE_LIMIT,
        settings.INTERACTION_RATE_WINDOW_SECONDS,
    )


InteractionRateLimit = Annotated[None, Depends(limit_interactions)]


async def limit_password_change(user: CurrentUser, redis: CurrentRedis) -> None:
    """Spend one change-password attempt, or raise 429 with the wait in seconds.

    `CurrentUser`, not `CurrentVerifiedUser`: securing an account (changing its
    password) must work regardless of email-verification status, same reasoning
    as the email-verification routes themselves. Superusers are exempt, matching
    `limit_interactions`. Setting `PASSWORD_CHANGE_RATE_LIMIT` to 0 disables it.
    """
    if user.is_superuser:
        return
    await enforce(
        redis,
        PASSWORD_CHANGE_SCOPE,
        str(user.id),
        settings.PASSWORD_CHANGE_RATE_LIMIT,
        settings.PASSWORD_CHANGE_RATE_WINDOW_SECONDS,
    )


async def limit_feedback(
    request: Request, user: OptionalUser, redis: CurrentRedis
) -> None:
    """Spend one feedback-submission slot, or raise 429 with the wait in seconds.

    Its own budget, and one that has to work for a caller with no account: the
    feedback form is reachable from the login screen (see app/api/feedback.py), so
    there is frequently no user id to key on. Signed in it keys on the user id like
    every other limiter; signed out it falls back to the caller's address (see
    `client_identity` for which address, and why).

    A signed-in user is keyed by id, so signing out is not a way around a spent
    budget - and, conversely, one busy office network cannot exhaust an identified
    user's own allowance.

    Superusers are exempt, matching the other limiters. Setting `FEEDBACK_RATE_LIMIT`
    to 0 disables it.
    """
    if user is not None and user.is_superuser:
        return
    identity = str(user.id) if user is not None else client_identity(request)
    await enforce(
        redis,
        FEEDBACK_SCOPE,
        identity,
        settings.FEEDBACK_RATE_LIMIT,
        settings.FEEDBACK_RATE_WINDOW_SECONDS,
    )


async def limit_login(request: Request, redis: CurrentRedis) -> None:
    """Spend one login attempt on the caller's address *and* one on the account
    being tried, or raise 429.

    Two budgets because they bound two different things. The per-address one
    bounds the CPU a single host can burn: every attempt costs an argon2 verify
    (64 MiB, on the event loop) whether or not the account exists, since
    `authenticate` deliberately hashes a dummy for unknown emails to keep timing
    uniform. The per-account one bounds the guesses one password can receive from
    *everywhere*, which no per-address limit can - a botnet gets a fresh allowance
    per host. The account identity is a hash of the normalized email, so neither
    the Redis key nor the log line carries an address.

    The email is read from the already-parsed form rather than by declaring
    `OAuth2PasswordRequestForm` here: this is attached to fastapi-users' whole
    auth router, and declaring the form would make `/logout` demand login fields.
    FastAPI parses a form body before it solves dependencies and Starlette caches
    the result on the request, so this costs no second parse.
    """
    await enforce(
        redis,
        LOGIN_SCOPE,
        client_identity(request),
        settings.LOGIN_RATE_LIMIT_PER_IP,
        settings.LOGIN_RATE_WINDOW_SECONDS,
    )
    if settings.LOGIN_RATE_LIMIT_PER_ACCOUNT <= 0:
        return
    form = await request.form()
    username = form.get("username")
    if not isinstance(username, str) or not username:
        return
    await enforce(
        redis,
        LOGIN_SCOPE,
        opaque_identity("account", username),
        settings.LOGIN_RATE_LIMIT_PER_ACCOUNT,
        settings.LOGIN_RATE_WINDOW_SECONDS,
    )


async def limit_register(request: Request, redis: CurrentRedis) -> None:
    """Spend one registration on the caller's address, or raise 429.

    There is no account to key on yet, so the address is the only handle. What it
    bounds is the mail: registration sends a verification code to an address the
    caller chose, so left open it is a way to send our mail to anyone, billed to
    us and rated against our sending domain.
    """
    await enforce(
        redis,
        REGISTER_SCOPE,
        client_identity(request),
        settings.REGISTER_RATE_LIMIT,
        settings.REGISTER_RATE_WINDOW_SECONDS,
    )


async def limit_account_export(user: CurrentUser, redis: CurrentRedis) -> None:
    """Spend one export slot, or raise 429 with the wait in seconds.

    `CurrentUser`, not `CurrentVerifiedUser`, for the same reason `DELETE
    /users/me` uses it: the right of access does not depend on having got round
    to clicking a link in an email. Superusers are exempt, matching every other
    limiter here. Setting `ACCOUNT_EXPORT_RATE_LIMIT` to 0 disables it.
    """
    if user.is_superuser:
        return
    await enforce(
        redis,
        ACCOUNT_EXPORT_SCOPE,
        str(user.id),
        settings.ACCOUNT_EXPORT_RATE_LIMIT,
        settings.ACCOUNT_EXPORT_RATE_WINDOW_SECONDS,
    )
