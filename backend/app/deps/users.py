import uuid
from typing import Annotated

from fastapi import Depends, Request
from fastapi_users import FastAPIUsers
from fastapi_users.authentication import (
    AuthenticationBackend,
    BearerTransport,
    JWTStrategy,
)
from fastapi_users.exceptions import InvalidPasswordException
from fastapi_users.manager import BaseUserManager, UUIDIDMixin
from fastapi_users_db_sqlalchemy import SQLAlchemyUserDatabase
from redis.asyncio import Redis

from app.core import email_verification as ev
from app.core.config import settings
from app.core.email import send_email
from app.core.errors import api_error
from app.core.password_policy import strength_violation
from app.deps.db import CurrentAsyncSession
from app.deps.redis import get_redis
from app.feed.service import earn_token
from app.models.user import User as UserModel

bearer_transport = BearerTransport(tokenUrl="auth/jwt/login")


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
        carrying `reason` as human-readable copy the client can render directly."""
        if not settings.REQUIRE_STRONG_PASSWORD:
            return
        reason = strength_violation(password)
        if reason:
            raise InvalidPasswordException(reason=reason)

    async def on_after_register(
        self, user: UserModel, request: Request | None = None
    ) -> None:
        """Grant the starting token balance so a brand-new account can publish a
        first post immediately, without having to review anything first, and (when
        REQUIRE_EMAIL_VERIFICATION is on) send the first verification code."""
        await earn_token(self._redis, str(user.id), settings.FEED_STARTING_TOKENS)
        if settings.REQUIRE_EMAIL_VERIFICATION:
            code = await ev.issue_code(self._redis, str(user.id))
            await ev.start_resend_cooldown(self._redis, str(user.id))
            subject, body = ev.email_content(code)
            await send_email(user.email, subject, body)

    async def _update(self, user: UserModel, update_dict: dict) -> UserModel:
        """Bump `settings_revision` when a settings field actually changes value.

        Hooked here rather than in a route because `PATCH /users/me` is served by
        fastapi-users' own router. Compares against the pre-update `user`, so a
        no-op PATCH (same value re-sent) doesn't inflate the revision and cause the
        client to see a phantom conflict.
        """
        if any(
            field in update_dict and getattr(user, field) != update_dict[field]
            for field in SETTINGS_FIELDS
        ):
            update_dict = {
                **update_dict,
                "settings_revision": user.settings_revision + 1,
            }
        return await super()._update(user, update_dict)


def get_user_db(session: CurrentAsyncSession):
    yield SQLAlchemyUserDatabase(session, UserModel)


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
