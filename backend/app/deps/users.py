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
from sqlalchemy.exc import IntegrityError

from app.core import email_verification as ev
from app.core import languages
from app.core.config import settings
from app.core.email import send_email
from app.core.locale import parse_accept_language
from app.core.errors import api_error
from app.core.logger import bind_request_context, get_logger
from app.core.password_policy import strength_violations
from app.core.username import is_username_taken
from app.deps.db import CurrentAsyncSession
from app.deps.redis import get_redis
from app.feed.service import earn_token
from app.models.oauth_account import OAuthAccount
from app.models.user import User as UserModel

log = get_logger(__name__)

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


#: The unique index behind `User.username`, named by Postgres. Only used to tell a
#: username clash apart from any other IntegrityError, which stays a 500.
_USERNAME_CONSTRAINT = "users_username_key"

#: User fields the mobile app mirrors into its local, offline-editable settings store.
#: Changing any of them bumps `User.settings_revision`; changing anything else
#: (bio, username, password) does not, because those are only ever edited online.
SETTINGS_FIELDS = frozenset({"dark_mode"})


def _locale_of(request: "Request | None") -> str:
    """The locale to write a mail in, from the request that triggered it.

    fastapi-users' hooks get the `Request` but no dependency injection, so
    `CurrentLocale` (app/deps/locale.py) is not available here - this is the same
    resolution one step lower down. `None` happens when a hook is invoked outside
    a request (a script, a test calling the manager directly), and falls back to
    DEFAULT_LOCALE like an absent header would.
    """
    if request is None:
        return settings.DEFAULT_LOCALE
    return parse_accept_language(request.headers.get("accept-language"))


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

        It also narrows the reader's accepted content languages from the column
        default (every configured language) to the one their request asked for. Done
        here rather than as a column default because only a request carries an
        `Accept-Language`, and done as a *narrowing* rather than the other way around
        so that any row reaching the database without passing through registration -
        a migration, a script, a fixture - keeps the wide default rather than being
        silently restricted to a language nobody chose for it.
        """
        await earn_token(self._redis, str(user.id), settings.FEED_STARTING_TOKENS)
        user.content_languages = languages.default_reading_languages(
            _locale_of(request)
        )
        await self.user_db.update(user, {"content_languages": user.content_languages})
        # Signups are the one number nobody wants to have to query the database
        # for, and this is also where a broken registration path shows up as a
        # gap rather than as an error.
        log.info(
            "user.registered",
            user_id=str(user.id),
            via="google" if user.oauth_accounts else "password",
            starting_tokens=settings.FEED_STARTING_TOKENS,
            content_languages=user.content_languages,
        )
        # `not user.is_verified` skips the code for Google signups, which arrive here
        # already verified (oauth_callback with is_verified_by_default) - Google has
        # confirmed the address, so mailing a code would be asking the user to prove
        # something we already know.
        if settings.REQUIRE_EMAIL_VERIFICATION and not user.is_verified:
            code = await ev.issue_code(self._redis, str(user.id))
            subject, body, html = ev.email_content(code, _locale_of(request))
            try:
                await send_email(user.email, subject, body, html)
            except Exception:
                # No address in the line: an email is personal data and the user
                # id resolves to one for whoever is entitled to look. This is an
                # ERROR because a user who never gets the code is stuck for good
                # (re-registering only yields `user_already_exists`).
                log.exception(
                    "email.verification_send_failed",
                    user_id=str(user.id),
                    on="register",
                )
            else:
                await ev.start_resend_cooldown(self._redis, str(user.id))

    async def on_after_login(
        self,
        user: UserModel,
        request: Request | None = None,
        response=None,
    ) -> None:
        """The only place a successful password login is recorded.

        Google sign-in does not pass through here - `POST /auth/google` mints its
        token directly - so it logs `auth.google_signin` itself.
        """
        # `via`, not `method`: the request context already carries the HTTP
        # method, and a field of the same name would shadow it on this line.
        log.info("auth.login", user_id=str(user.id), via="password")

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
            # No id to name and deliberately not the address that was tried:
            # `client_ip` is already on the line from the request context, which
            # is what makes a burst of these attributable at all.
            log.info("auth.login_failed", reason="unknown_account")
            return None
        if existing.oauth_accounts:
            log.info(
                "auth.login_failed", reason="google_account", user_id=str(existing.id)
            )
            raise api_error(400, "login_use_google")
        # The line worth counting: repeated failures against an account that
        # exists is what password guessing looks like from here.
        log.info("auth.login_failed", reason="bad_password", user_id=str(existing.id))
        return None

    async def _check_username_free(
        self, username: str | None, *, exclude_user_id: uuid.UUID | None = None
    ) -> None:
        """Refuse a username someone else already holds, with a code the client
        can render under the field.

        `User.username` is unique, so without this the INSERT/UPDATE raises an
        IntegrityError that nothing catches and the user gets a bare 500 - a
        registration form saying "something went wrong" about the one thing they
        could have fixed themselves. Checked here rather than in a route because
        both writers (`POST /auth/register` and fastapi-users' own
        `PATCH /users/me` / `PATCH /users/{id}`) go through this manager.
        """
        if username is None:
            return
        session = self.user_db.session
        if await is_username_taken(session, username, exclude_user_id=exclude_user_id):
            # The name itself stays out of the line - it is user-chosen content,
            # and `user_id` is already bound on an authenticated request. Worth
            # counting: a registration form that keeps refusing names is the kind
            # of drop-off nobody would otherwise see.
            log.info(
                "user.username_taken",
                on="update" if exclude_user_id else "register",
            )
            raise api_error(409, "username_taken")

    async def _conflict_as_api_error(self, exc: IntegrityError) -> None:
        """Turn the lost race into the same refusal as the pre-check.

        `_check_username_free` is a probe, not a reservation, so two signups
        claiming one name in the same instant both pass it and one loses at the
        constraint. Same answer either way; anything else is a genuine bug and is
        re-raised. The session has to be rolled back first - it is aborted after a
        failed flush, and it outlives this call (the request still has to render
        the error response through it).
        """
        await self.user_db.session.rollback()
        # asyncpg names the constraint on the exception; the string match is the
        # fallback for drivers/wrappers that only put it in the message.
        constraint = getattr(exc.orig, "constraint_name", None)
        if constraint != _USERNAME_CONSTRAINT and _USERNAME_CONSTRAINT not in str(
            exc.orig
        ):
            raise exc
        log.info("user.username_taken", on="race")
        raise api_error(409, "username_taken")

    async def create(self, user_create, safe: bool = False, request=None):
        """Reject a taken username before fastapi-users writes the row."""
        await self._check_username_free(getattr(user_create, "username", None))
        try:
            return await super().create(user_create, safe=safe, request=request)
        except IntegrityError as exc:
            await self._conflict_as_api_error(exc)

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
        change, and revoking `is_verified` on an email change - plus the one guard
        that refuses a write outright, a username someone else holds.

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
        if "username" in update_dict and update_dict["username"] != user.username:
            await self._check_username_free(
                update_dict["username"], exclude_user_id=user.id
            )
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
            # Neither address is logged, only that the account moved: an email
            # change that revokes verification is the start of most "I can no
            # longer get in" reports, so it wants to be findable.
            log.info("user.email_changed", user_id=str(user.id))
        try:
            return await super()._update(user, update_dict)
        except IntegrityError as exc:
            await self._conflict_as_api_error(exc)

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
        subject, body, html = ev.email_content(code, _locale_of(request))
        try:
            await send_email(user.email, subject, body, html)
        except Exception:
            # The email change itself is already committed, so failing the request
            # now would tell the client nothing happened when in fact everything
            # did. Leave the cooldown unset instead, so "resend" is immediately
            # available on the screen the user is about to land on.
            log.exception(
                "email.verification_send_failed",
                user_id=str(user.id),
                on="email_change",
            )
            return
        await ev.start_resend_cooldown(self._redis, str(user.id))


def get_user_db(session: CurrentAsyncSession):
    yield SQLAlchemyUserDatabase(session, UserModel, OAuthAccount)


def get_user_manager(user_db=Depends(get_user_db), redis: Redis = Depends(get_redis)):
    yield UserManager(user_db, redis)


fastapi_users = FastAPIUsers(get_user_manager, [jwt_authentication])


async def _bind_current_user(
    user: Annotated[UserModel, Depends(fastapi_users.current_user(active=True))],
) -> UserModel:
    """Put the caller's id into the log context for the rest of the request.

    Wrapping the dependency is what makes `user_id` appear on every line a route
    produces - the access-log line included, which is written after the response
    and would otherwise never see it - without a single route having to pass the
    user into a log call. Superuser and optional variants below do the same, so
    the only routes without a user id on their lines are the ones that genuinely
    have no user (login, register, signed-out feedback) and fastapi-users' own
    `/users/me` router, which builds its dependency itself.
    """
    bind_request_context(user_id=str(user.id))
    return user


async def _bind_current_superuser(
    user: Annotated[
        UserModel, Depends(fastapi_users.current_user(active=True, superuser=True))
    ],
) -> UserModel:
    bind_request_context(user_id=str(user.id), superuser=True)
    return user


async def _bind_optional_user(
    user: Annotated[
        UserModel | None,
        Depends(fastapi_users.current_user(active=True, optional=True)),
    ],
) -> UserModel | None:
    if user is not None:
        bind_request_context(user_id=str(user.id))
    return user


CurrentUser = Annotated[UserModel, Depends(_bind_current_user)]
CurrentSuperuser = Annotated[UserModel, Depends(_bind_current_superuser)]


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

# For routes that work signed in *and* signed out, and behave differently
# depending. Currently only feedback (app/api/feedback.py), which is linked from
# the login screen precisely so "I can't sign in" is reportable.
#
# `optional=True` makes fastapi-users yield None instead of raising for a missing
# token - and also for an *expired or malformed* one, which is the behaviour to be
# aware of: a route using this must never treat None as "definitely a stranger" in
# a way that would silently downgrade a signed-in user's request. Feedback's
# handling of that is to force anonymity, which fails in the safe direction.
OptionalUser = Annotated[UserModel | None, Depends(_bind_optional_user)]
