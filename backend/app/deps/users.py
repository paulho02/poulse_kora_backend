import logging
import uuid
from typing import Annotated

from fastapi import Depends, Request
from fastapi_users import FastAPIUsers
from fastapi_users.authentication import (
    AuthenticationBackend,
    BearerTransport,
    JWTStrategy,
)
from fastapi_users.exceptions import InvalidPasswordException, UserNotExists
from fastapi_users.manager import BaseUserManager, UUIDIDMixin
from fastapi_users_db_sqlalchemy import SQLAlchemyUserDatabase
from redis.asyncio import Redis

from app.core import email_verification as ev
from app.core.config import settings
from app.core.email import send_email
from app.core.errors import api_error
from app.core.password_policy import strength_violations
from app.deps.db import CurrentAsyncSession
from app.deps.redis import get_redis
from app.feed.service import earn_token
from app.models.oauth_account import OAuthAccount
from app.models.user import User as UserModel

logger = logging.getLogger(__name__)

bearer_transport = BearerTransport(tokenUrl=f"{settings.API_PATH}/auth/jwt/login")


def get_jwt_strategy() -> JWTStrategy:
    return JWTStrategy(
        secret=settings.SECRET_KEY,
        lifetime_seconds=settings.ACCESS_TOKEN_EXPIRE_MINUTES * 60,
    )


jwt_authentication = AuthenticationBackend(
    name="jwt",
    transport=bearer_transport,
    get_strategy=get_jwt_strategy,
)


#: User fields the mobile app mirrors into its local, offline-editable settings store.
#: Changing any of them bumps `User.settings_revision`; changing anything else
#: (bio, username, password) does not, because those are only ever edited online.
SETTINGS_FIELDS = frozenset({"dark_mode"})


class UserManager(UUIDIDMixin, BaseUserManager[UserModel, uuid.UUID]):
    reset_password_token_secret = settings.SECRET_KEY
    verification_token_secret = settings.SECRET_KEY

    def __init__(self, user_db, redis: Redis):
        super().__init__(user_db)
        self._redis = redis

    async def validate_password(self, password: str, user) -> None:
        """Enforced only when REQUIRE_STRONG_PASSWORD is on (see
        app.core.password_policy) - off means fastapi-users applies no rule at all,
        by design. Raising here is what turns into the structured
        `register_invalid_password` / `update_user_invalid_password` API error,
        carrying `reason` as a list of `{code, params}` violations the client
        localizes (see `lib/src/core/errors/error_messages.dart`)."""
        if not settings.REQUIRE_STRONG_PASSWORD:
            return
        violations = strength_violations(password)
        if violations:
            raise InvalidPasswordException(reason=violations)

    async def on_after_register(
        self, user: UserModel, request: Request | None = None
    ) -> None:
        """Grant the starting token balance so a brand-new account can publish a
        first post immediately, without having to review anything first, and (when
        REQUIRE_EMAIL_VERIFICATION is on) send the first verification code.

        The send is best-effort: the account is already created by this point, so
        a transient SMTP failure must not blow up `POST /auth/register` with a
        500 (the user would then be stuck - re-registering just gets
        `user_already_exists`). The cooldown is only started once the send
        actually succeeds, so a failed first attempt doesn't lock the user out of
        an immediate retry via `/auth/email-verification/resend`.
        """
        await earn_token(self._redis, str(user.id), settings.FEED_STARTING_TOKENS)
        # `not user.is_verified` skips the code for Google signups, which arrive here
        # already verified (oauth_callback with is_verified_by_default) - Google has
        # confirmed the address, so mailing a code would be asking the user to prove
        # something we already know.
        if settings.REQUIRE_EMAIL_VERIFICATION and not user.is_verified:
            code = await ev.issue_code(self._redis, str(user.id))
            subject, body = ev.email_content(code)
            try:
                await send_email(user.email, subject, body)
            except Exception:
                logging.getLogger(__name__).exception(
                    "Failed to send verification email to %s on register", user.email
                )
            else:
                await ev.start_resend_cooldown(self._redis, str(user.id))

    async def authenticate(self, credentials):
        """Tell a Google account apart from a wrong password.

        Linking to Google overwrites `hashed_password` with a random value nobody
        holds (see app/api/google_auth.py), so a Google user typing their old
        password lands in the ordinary failure path below and would otherwise be told
        their credentials are bad - true, but useless: they would keep retrying a
        password that can never work again. `login_use_google` points them at the
        button instead.

        This does confirm to an unauthenticated caller that an account exists for
        that email. Accepted: `POST /auth/register` already discloses exactly the
        same fact via `register_user_already_exists`.
        """
        user = await super().authenticate(credentials)
        if user is not None:
            return user

        # Only on the failure path, so a successful login costs no extra query.
        try:
            existing = await self.get_by_email(credentials.username)
        except UserNotExists:
            return None
        if existing.oauth_accounts:
            raise api_error(400, "login_use_google")
        return None

    async def update(self, user_update, user: UserModel, safe: bool = False, request=None):
        """`PATCH /users/me` is fastapi-users' own stock route, and `BaseUserUpdate`
        exposes a bare `password` field with no proof the caller knows the current
        one - anyone holding a valid token (a leaked/stolen one included) could
        otherwise silently change the account password. Password changes must go
        through `POST /auth/change-password` (app/api/change_password.py) instead,
        which does require it; refuse one here rather than leave that route as a
        silent bypass around it.
        """
        if user_update.password is not None:
            raise InvalidPasswordException(
                reason=[{"code": "password_change_wrong_endpoint", "params": {}}]
            )
        # Note there is deliberately no Google-account email lock here. A Google
        # account is bound to its identity by `sub`, not by address (see
        # app/api/google_auth.py), so `email` is just the contact address and stays
        # as editable as it is on a password account.
        return await super().update(user_update, user, safe=safe, request=request)

    async def _update(self, user: UserModel, update_dict: dict) -> UserModel:
        """Derive the fields a write implies: `settings_revision` on a settings
        change, and revoking `is_verified` on an email change.

        Hooked here rather than in a route because `PATCH /users/me` is served by
        fastapi-users' own router, and because this is the single chokepoint every
        update goes through - including a superuser editing someone else via
        `PATCH /users/{id}`. Both checks compare against the pre-update `user`, so a
        no-op PATCH (same value re-sent) neither inflates the revision nor kicks the
        account back into verification.
        """
        if any(
            field in update_dict and getattr(user, field) != update_dict[field]
            for field in SETTINGS_FIELDS
        ):
            update_dict = {
                **update_dict,
                "settings_revision": user.settings_revision + 1,
            }
        # `is_verified` asserts one specific thing: that this person can read mail
        # at `user.email`. It is proof about an *address*, not about an account, so
        # pointing the account at a different address invalidates it - otherwise
        # anyone could verify an address they own, then switch to one they don't and
        # keep the access that proof bought them.
        #
        # Deliberately unconditional, including when the new address happens to be
        # the linked Google one (which Google has in fact verified). Re-proving in
        # that corner costs the user one code; special-casing it would put a second
        # path to `is_verified = True` in a place nobody would think to audit.
        if "email" in update_dict and update_dict["email"] != user.email:
            update_dict = {**update_dict, "is_verified": False}
        return await super()._update(user, update_dict)

    async def on_after_update(
        self, user: UserModel, update_dict: dict, request: Request | None = None
    ) -> None:
        """Mail a fresh verification code after an email change.

        `_update` has just revoked `is_verified`, so the client is about to be
        redirected to the verify-email screen. That screen does send a code the
        moment it opens, but only via `/auth/email-verification/resend`, which
        no-ops under an active cooldown - there is none yet for this address, so
        without this the client's own send would be the first one anyway. Sending
        it here instead just avoids a redundant round trip and keeps the code
        landing in the user's inbox the instant the email changes, not once the
        screen happens to mount.

        `update_dict` is empty when this fires from `oauth_associate_callback`
        (app/api/google_auth.py), which is why the guard is on the key rather than
        on the user's state alone.
        """
        if not settings.REQUIRE_EMAIL_VERIFICATION:
            return
        if "email" not in update_dict or user.is_verified:
            return

        code = await ev.issue_code(self._redis, str(user.id))
        subject, body = ev.email_content(code)
        try:
            await send_email(user.email, subject, body)
        except Exception:
            # The email change itself is already committed, so failing the request
            # now would tell the client nothing happened when in fact everything
            # did. Leave the cooldown unset instead, so "resend" is immediately
            # available on the screen the user is about to land on.
            logger.exception("Could not send verification code to %s", user.email)
            return
        await ev.start_resend_cooldown(self._redis, str(user.id))


def get_user_db(session: CurrentAsyncSession):
    yield SQLAlchemyUserDatabase(session, UserModel, OAuthAccount)


def get_user_manager(user_db=Depends(get_user_db), redis: Redis = Depends(get_redis)):
    yield UserManager(user_db, redis)


fastapi_users = FastAPIUsers(get_user_manager, [jwt_authentication])

CurrentUser = Annotated[UserModel, Depends(fastapi_users.current_user(active=True))]
CurrentSuperuser = Annotated[
    UserModel, Depends(fastapi_users.current_user(active=True, superuser=True))
]


async def get_verified_user(user: CurrentUser) -> UserModel:
    """Gate for the feed/social routes (posts, channels, items, stats) - the
    "actions" REQUIRE_EMAIL_VERIFICATION talks about. Deliberately not built on
    fastapi-users' own `current_user(verified=True)`: that raises a bare 401/403
    with no body detail at all, which our exception handler would slugify from the
    HTTP reason phrase ("Forbidden") into a generic, unmappable error code - `api_error`
    gives the client a stable `unverified_user` code to key its UI off instead.

    Superusers bypass this, same as they bypass rate limiting and token costs.
    """
    requires_verification = settings.REQUIRE_EMAIL_VERIFICATION
    if requires_verification and not user.is_verified and not user.is_superuser:
        raise api_error(403, "unverified_user")
    return user


CurrentVerifiedUser = Annotated[UserModel, Depends(get_verified_user)]
