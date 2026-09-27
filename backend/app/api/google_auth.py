"""Google sign-in.

The client obtains a Google ID token itself (the `google_sign_in` plugin, on
Android and web) and posts it here; we verify it and mint our own JWT. Deliberately
not fastapi-users' `get_oauth_router`: that is a browser redirect flow - which would
mean deep links and custom URL schemes the app has none of - and its
`associate_by_email` linking is silent and unconditional, leaving nowhere to put the
confirmation step below. The account handling underneath is still fastapi-users'
own (`oauth_callback` / `oauth_associate_callback` and the `oauth_account` table).

Three rules govern account identity, and every guard here implements one of them:

1. A password account whose address matches a Google account can be **upgraded**,
   but only after the user confirms: the first request answers 409
   `google_link_required`, and the client re-sends the same ID token with
   `link_existing: true`.
2. The upgrade is **irreversible** - it destroys the password (`_disable_password`).
   Password login and `POST /auth/change-password` refuse from then on (see
   app/deps/users.py and app/api/change_password.py). Which is why the signed-in
   path, `/auth/google/link`, asks for the current password first.
3. Registering with an address already held by a Google account gets the ordinary
   `register_user_already_exists`, unchanged - nothing here is involved.

Email is a *contact address*, not an identity. A Google identity is bound to an
account by its `sub`, and `/auth/google/link` will attach a Google account whose
address differs from the account's own, leaving `User.email` alone. Email only
enters the picture in the login-screen upgrade path above, as the hint that lets
us offer to link an account the user has not told us about yet.
"""

from fastapi import APIRouter, Depends, Response, status
from fastapi_users.authentication.transport.bearer import BearerResponse
from sqlalchemy.exc import IntegrityError

from app.core.config import settings
from app.core.errors import api_error
from app.core.google_oauth import GoogleIdentity, verify_google_id_token
from app.core.logger import bind_request_context, get_logger
from app.core.username import generate_unique_username
from app.deps.db import CurrentAsyncSession
from app.deps.rate_limit import limit_password_change
from app.deps.users import (
    CurrentUser,
    UserManager,
    get_jwt_strategy,
    get_user_manager,
    jwt_authentication,
)
from app.models.oauth_account import GOOGLE_OAUTH_NAME
from app.models.user import User
from app.schemas.user import GoogleAuthRequest, GoogleLinkRequest, UserRead

log = get_logger(__name__)

router = APIRouter(prefix="/auth", tags=["auth"])

OAUTH_NAME = GOOGLE_OAUTH_NAME

#: `OAuthAccount.access_token` is non-nullable, but we have no use for a Google
#: token: we never call a Google API, we only authenticate. Storing the ID token
#: here would keep a bearer credential at rest for nothing, so store nothing.
NO_STORED_TOKEN = ""


def _disable_password(user_manager: UserManager, user: User) -> None:
    """Make the account's password permanently unusable.

    Uses the same primitive fastapi-users' own `oauth_callback` applies to accounts
    it creates from scratch, so an upgraded account ends up indistinguishable from a
    Google-native one: a random password nobody has ever seen. Not a nullable column
    and not a sentinel value, so no code path can accidentally read it as "no
    password set" and let it be bypassed.
    """
    user.hashed_password = user_manager.password_helper.hash(
        user_manager.password_helper.generate()
    )


async def _issue_token(user: User) -> Response:
    """Same response body as `POST /auth/jwt/login`, produced by the same backend, so
    the client can parse both with one code path."""
    return await jwt_authentication.login(get_jwt_strategy(), user)


def _require_enabled() -> None:
    if not settings.GOOGLE_OAUTH_ENABLED:
        raise api_error(400, "google_oauth_disabled")


async def _assign_username(
    session: CurrentAsyncSession, user: User, identity: GoogleIdentity
) -> None:
    """Give a freshly created Google account a username.

    `oauth_callback` knows nothing about our `username` column and leaves it NULL.
    The generated name is a starting point, not a final answer - the app's onboarding
    username step shows it pre-filled and lets the user change it.
    """
    for _ in range(2):
        user.username = await generate_unique_username(
            session, seed=identity.name or identity.email.split("@")[0]
        )
        try:
            await session.commit()
            return
        except IntegrityError:
            # Another signup took the same derived name between the probe and this
            # UPDATE. Roll back and derive again; the second pass sees it taken.
            await session.rollback()
    # Two collisions in a row is vanishingly unlikely and not worth failing a signup
    # over - leave the username NULL and let onboarding collect one.
    await session.rollback()


@router.post(
    "/google",
    response_model=BearerResponse,
    responses={409: {"description": "Address already held by a password account"}},
)
async def google_auth(
    body: GoogleAuthRequest,
    session: CurrentAsyncSession,
    user_manager: UserManager = Depends(get_user_manager),
):
    """Sign in with Google, creating or upgrading an account as needed."""
    _require_enabled()
    identity = await verify_google_id_token(body.id_token)
    user_db = user_manager.user_db

    linking = False
    existing = await user_db.get_by_oauth_account(OAUTH_NAME, identity.subject)
    if existing is None:
        by_email = await user_db.get_by_email(identity.email)
        if by_email is not None:
            if by_email.oauth_accounts:
                # The address is held by an account already linked to a *different*
                # Google identity - typically someone whose Google address was
                # reassigned. Attaching a second link would silently give two Google
                # accounts access to one account here; refusing is the safe answer.
                raise api_error(400, "google_account_mismatch")
            if not body.link_existing:
                # Rule 1. No server-side state is needed for the second pass: Google
                # ID tokens live about an hour, so the client simply re-sends this
                # same one once the user has confirmed.
                raise api_error(409, "google_link_required", email=by_email.email)
            linking = True

    user = await user_manager.oauth_callback(
        OAUTH_NAME,
        NO_STORED_TOKEN,
        identity.subject,
        identity.email,
        associate_by_email=True,
        is_verified_by_default=True,
    )

    if linking:
        # `oauth_callback`'s associate branch only attaches the link; it does not
        # touch `is_verified` (that flag is for accounts it creates). Google has
        # confirmed the address, so an account still waiting on an email code is
        # done waiting.
        user.is_verified = True
        _disable_password(user_manager, user)
        await session.commit()
    elif existing is None:
        await _assign_username(session, user, identity)

    if not user.is_active:
        raise api_error(400, "login_bad_credentials")
    bind_request_context(user_id=str(user.id))
    # `outcome` is the field worth having: "linked" is the irreversible upgrade
    # (the password is destroyed by it), and it is the one branch a support
    # question can hinge on months later.
    log.info(
        "auth.google_signin",
        user_id=str(user.id),
        outcome="linked" if linking else ("existing" if existing else "created"),
    )
    return await _issue_token(user)


@router.post(
    "/google/link",
    response_model=UserRead,
    status_code=status.HTTP_200_OK,
    dependencies=[Depends(limit_password_change)],
)
async def link_google(
    body: GoogleLinkRequest,
    user: CurrentUser,
    session: CurrentAsyncSession,
    user_manager: UserManager = Depends(get_user_manager),
):
    """Upgrade the signed-in account to Google sign-in (Profile -> Settings).

    Same irreversible upgrade as the confirmed branch of `POST /auth/google`, but
    the Google account **does not have to use the same address**. Here the two are
    deliberately separate concerns:

    - the Google identity is the *credential* - what you sign in with, matched on
      `sub`, never on email;
    - `User.email` stays the account's *contact address*, untouched, so an account
      created with a work address keeps receiving mail there after linking a
      personal Google account.

    That only holds because sign-in resolves by `sub`: `POST /auth/google` looks
    the account up via `get_by_oauth_account` before it ever considers email. The
    one invariant that has to be defended is therefore that a Google identity maps
    to at most one account - `google_account_in_use` below - since two would leave
    a later sign-in with no way to tell which was meant.

    No confirmation handshake, unlike the login-screen path: being signed in
    already establishes intent, and the client shows the warning before calling.

    **The current password is required**, as for `POST /auth/change-password` and
    `DELETE /users/me`: this destroys the password, so a stolen token alone would
    otherwise let an attacker bind *their* Google account and leave the owner no
    way back in. The login-screen upgrade needs no such check - there, the Google
    account has to hold the account's own address, which is proof of its own.
    Shares the change-password budget, since it tests the same secret.
    """
    _require_enabled()

    if user.oauth_accounts:
        raise api_error(400, "google_already_linked")

    # Before the Google token is verified: a wrong password must not learn
    # anything about the token, and a refused request costs no call to Google.
    if not body.current_password:
        raise api_error(400, "google_link_password_required")
    verified, _ = user_manager.password_helper.verify_and_update(
        body.current_password, user.hashed_password
    )
    if not verified:
        # WARNING, like its change-password and delete-account siblings: this is
        # the route a stolen token would be used on.
        log.warning("auth.google_link_failed", reason="wrong_password")
        raise api_error(400, "google_link_wrong_password")

    identity = await verify_google_id_token(body.id_token)

    other = await user_manager.user_db.get_by_oauth_account(
        OAUTH_NAME, identity.subject
    )
    if other is not None:
        raise api_error(400, "google_account_in_use")

    user = await user_manager.oauth_associate_callback(
        user, OAUTH_NAME, NO_STORED_TOKEN, identity.subject, identity.email
    )
    # Only when the two addresses actually match. Google vouched for *its* address;
    # if the account's contact address is a different one, we have learned nothing
    # about it, and flipping is_verified would hand out a free pass around email
    # verification for an address the user may not own.
    if identity.email.lower() == user.email.lower():
        user.is_verified = True
    _disable_password(user_manager, user)
    await session.commit()
    log.info(
        "auth.google_linked",
        user_id=str(user.id),
        # Whether the Google address matched the account's own is exactly what
        # decides `is_verified` here, so it is the field to record - not either
        # address itself.
        addresses_match=identity.email.lower() == user.email.lower(),
    )
    return user
