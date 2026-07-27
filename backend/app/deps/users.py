import uuid
from typing import Annotated

from fastapi import Depends
from fastapi_users import FastAPIUsers
from fastapi_users.authentication import (
    AuthenticationBackend,
    BearerTransport,
    JWTStrategy,
)
from fastapi_users.manager import BaseUserManager, UUIDIDMixin
from fastapi_users_db_sqlalchemy import SQLAlchemyUserDatabase

from app.core.config import settings
from app.deps.db import CurrentAsyncSession
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


def get_user_manager(user_db=Depends(get_user_db)):
    yield UserManager(user_db)


fastapi_users = FastAPIUsers(get_user_manager, [jwt_authentication])

CurrentUser = Annotated[UserModel, Depends(fastapi_users.current_user(active=True))]
CurrentSuperuser = Annotated[
    UserModel, Depends(fastapi_users.current_user(active=True, superuser=True))
]
