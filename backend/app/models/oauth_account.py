from fastapi_users_db_sqlalchemy import SQLAlchemyBaseOAuthAccountTableUUID
from fastapi_users_db_sqlalchemy.generics import GUID
from sqlalchemy import ForeignKey
from sqlalchemy.orm import Mapped, declared_attr, mapped_column

from app.db import Base

#: Value stored in `OAuthAccount.oauth_name` for Google. Lives here rather than in
#: app/api/google_auth.py so `User` can read it without importing the API layer.
GOOGLE_OAUTH_NAME = "google"


class OAuthAccount(SQLAlchemyBaseOAuthAccountTableUUID, Base):
    """Link between a `User` and an external identity provider account.

    Straight from fastapi-users' own base table (id, user_id, oauth_name,
    access_token, expires_at, refresh_token, account_id, account_email), so
    `SQLAlchemyUserDatabase(session, User, OAuthAccount)` can serve
    `get_by_oauth_account` / `add_oauth_account` and `UserManager.oauth_callback`
    works unmodified - see app/api/google_auth.py.

    Only Google writes rows here today (`oauth_name == "google"`), but the shape is
    the provider-agnostic one, so adding a second provider needs no schema change.

    The presence of *any* row is what makes an account a Google account: it drives
    `User.auth_provider`, and with it the refusal of password login, password change
    and email change. Nothing ever deletes a row - the upgrade is one-way by design.
    """

    __tablename__ = "oauth_account"

    @declared_attr
    def user_id(cls) -> Mapped[GUID]:
        """Overridden solely to retarget the foreign key.

        The base class points at `user.id`, but this project's user table is
        `users` (see `User.__tablename__`); left inherited, metadata resolution
        fails outright with NoReferencedTableError.

        Indexed, which the base class does not do: `User.oauth_accounts` is a
        `selectin` load, so every authenticated request looks this column up.
        """
        return mapped_column(
            GUID,
            ForeignKey("users.id", ondelete="cascade"),
            nullable=False,
            index=True,
        )
