from datetime import datetime
from typing import TYPE_CHECKING

from fastapi_users_db_sqlalchemy import SQLAlchemyBaseUserTableUUID
from sqlalchemy import DateTime
from sqlalchemy.orm import Mapped, mapped_column, relationship
from sqlalchemy.sql.functions import func

from app.core.config import settings
from app.db import Base
from app.models.oauth_account import GOOGLE_OAUTH_NAME

if TYPE_CHECKING:
    from app.models.channel_subscription import ChannelSubscription  # noqa: F401
    from app.models.item import Item  # noqa: F401
    from app.models.oauth_account import OAuthAccount  # noqa: F401
    from app.models.post import Post  # noqa: F401
    from app.models.post_review import PostReview  # noqa: F401
    from app.models.user_subscription import UserSubscription  # noqa: F401


class User(SQLAlchemyBaseUserTableUUID, Base):
    __tablename__ = "users"

    created: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    updated: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )

    username: Mapped[str | None] = mapped_column(unique=True)
    bio: Mapped[str | None]
    dark_mode: Mapped[bool] = mapped_column(default=False, server_default="false")

    # Stored directly in the database - there is no file storage yet (see
    # PROFILE_PICTURE_* in app/core/config.py for the size/type limits enforced on
    # upload, app/api/users.py for the upload/fetch routes). `profile_picture_url`
    # below is what actually gets exposed on UserRead/PostAuthor, never these bytes.
    profile_picture: Mapped[bytes | None]
    profile_picture_content_type: Mapped[str | None]

    # Flipped once, after the mobile app's one-time onboarding flow (intro slides,
    # mandatory channel picks, disclaimer) is confirmed. A plain one-way flag: unlike
    # `dark_mode` it has no offline-conflict scenario, so it isn't part of
    # SETTINGS_FIELDS/settings_revision.
    onboarding_completed: Mapped[bool] = mapped_column(
        default=False, server_default="false"
    )

    # Bumped by UserManager._update whenever a *settings* field (see SETTINGS_FIELDS
    # in app/deps/users.py) actually changes value. The mobile app keeps the revision
    # it last reconciled with, so it can tell "nobody else touched this, safe to push
    # my offline change" from "another device changed it too".
    #
    # This exists instead of reusing `updated` because `updated` is a whole-row
    # onupdate: reviewing a post bumps reviewed_count and would therefore look like a
    # settings conflict on every single forward/drop. It also avoids comparing a
    # server clock against a device clock.
    settings_revision: Mapped[int] = mapped_column(default=0, server_default="0")

    # Denormalized counters, updated transactionally alongside `PostReview` inserts
    # (see app/core/relay_rules.py) so the review-gate check is an O(1) attribute read.
    reviewed_count: Mapped[int] = mapped_column(default=0, server_default="0")
    forwarded_count: Mapped[int] = mapped_column(default=0, server_default="0")
    dropped_count: Mapped[int] = mapped_column(default=0, server_default="0")

    items: Mapped[list["Item"]] = relationship(
        back_populates="user", cascade="all, delete"
    )
    channel_subscriptions: Mapped[list["ChannelSubscription"]] = relationship(
        back_populates="user", cascade="all, delete"
    )
    posts: Mapped[list["Post"]] = relationship(
        back_populates="author", cascade="all, delete"
    )
    post_reviews: Mapped[list["PostReview"]] = relationship(
        back_populates="user", cascade="all, delete"
    )
    subscriptions: Mapped[list["UserSubscription"]] = relationship(
        back_populates="user", cascade="all, delete"
    )

    # Eager-loaded because `auth_provider` below is read while serializing UserRead
    # and inside several route guards, and a lazy attribute access is an error under
    # async SQLAlchemy. `selectin` rather than the `joined` fastapi-users' docs show:
    # a joined eager load *against a collection* obliges every `select(User)` in the
    # codebase to call `.unique()` on its result or raise at runtime - which broke
    # `GET /users` and `rebuild_from_pg` the moment this was added, and would quietly
    # wait to break the next such query somebody writes. `selectin` costs one extra
    # indexed lookup on a tiny table instead, and carries no such rule.
    oauth_accounts: Mapped[list["OAuthAccount"]] = relationship(
        "OAuthAccount", lazy="selectin", cascade="all, delete"
    )

    @property
    def auth_provider(self) -> str:
        """How this account signs in: "google" once any OAuth account is linked,
        "password" otherwise.

        Linking is one-way and irreversible (see app/api/google_auth.py), so this
        flipping to "google" is what closes the routes back to password auth:
        `POST /auth/jwt/login` and `POST /auth/change-password` refuse from here on.
        Exposed on `UserRead` so the client can hide those affordances rather than
        let the user discover the refusal.

        `email` is *not* one of them: it is the contact address, not the credential,
        and stays editable via `PATCH /users/me`. See `google_email` below for the
        address that actually signs this account in.
        """
        return "google" if self.oauth_accounts else "password"

    @property
    def google_email(self) -> str | None:
        """Address of the linked Google account, or None if there isn't one.

        Deliberately *not* the same thing as `email`: linking accepts a Google
        account whose address differs (see app/api/google_auth.py), so after that
        the account has two addresses — this one to sign in with, `email` to be
        contacted at. Exposed on `UserRead` because otherwise nothing in the UI
        could tell the user which Google account actually signs them in.
        """
        for account in self.oauth_accounts:
            if account.oauth_name == GOOGLE_OAUTH_NAME:
                return account.account_email
        return None

    @property
    def profile_picture_url(self) -> str | None:
        """URL to fetch this user's profile picture bytes (`GET
        /users/{id}/profile-picture`), or None if none is set.

        A URL rather than embedding the bytes/base64 directly on UserRead/PostAuthor:
        a feed lists many posts, often several by the same author, and repeating the
        full image on every one would bloat every feed response. A URL lets the
        client fetch (and cache) the image once per author instead.
        """
        if self.profile_picture is None:
            return None
        return f"{settings.API_PATH}/users/{self.id}/profile-picture"

    def __repr__(self):
        return f"User(id={self.id!r}, name={self.email!r})"
