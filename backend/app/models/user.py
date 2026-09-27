from datetime import datetime
from typing import TYPE_CHECKING

from fastapi_users_db_sqlalchemy import SQLAlchemyBaseUserTableUUID
from sqlalchemy import ARRAY, CheckConstraint, DateTime, String
from sqlalchemy.orm import Mapped, mapped_column, relationship
from sqlalchemy.sql.functions import func

from app.core.config import settings
from app.core.storage import storage
from app.db import Base
from app.models.oauth_account import GOOGLE_OAUTH_NAME

if TYPE_CHECKING:
    from app.models.channel_subscription import ChannelSubscription  # noqa: F401
    from app.models.item import Item  # noqa: F401
    from app.models.oauth_account import OAuthAccount  # noqa: F401
    from app.models.post import Post  # noqa: F401
    from app.models.post_review import PostReview  # noqa: F401
    from app.models.probe_response import ProbeResponse  # noqa: F401
    from app.models.user_subscription import UserSubscription  # noqa: F401


class User(SQLAlchemyBaseUserTableUUID, Base):
    __tablename__ = "users"
    # The character half of app/core/username_policy.py, as a backstop for any
    # writer that bypasses `UserManager` (scripts, the probe author). Length stays
    # in settings, so it is not pinned here. Autogenerate does not compare CHECK
    # constraints; alembic 0007 is where it lives on a real database.
    __table_args__ = (
        CheckConstraint(
            "username ~ '^[a-z0-9_]+$'", name="ck_users_username_charset"
        ),
    )

    created: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    updated: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )

    username: Mapped[str | None] = mapped_column(unique=True)
    bio: Mapped[str | None]
    dark_mode: Mapped[bool] = mapped_column(default=False, server_default="false")

    # Languages this reader accepts posts in - the other half of the feed's routing
    # key (see app/feed/keys.py: audience, and app/core/languages.py). Mirrored into
    # Redis as one audience-set membership per (subscribed channel x language), which
    # is what keeps fan-out's recipient sampling O(sample) instead of an intersection
    # per operation.
    #
    # A Postgres array rather than a join table: it is read whole or not at all,
    # nothing ever queries "who accepts German" (Redis answers that), and a table
    # would add a row to unwind in account deletion for no query it enables. Never
    # empty - an empty set is an audience of nowhere and so a permanently empty feed;
    # `sanitize_reading_languages` is the one writer and refuses to produce one.
    #
    # Not in SETTINGS_FIELDS, because it is not written through `UserManager._update`:
    # changing it has to rewrite Redis memberships, so it has its own route
    # (PUT /users/me/content-languages) which bumps `settings_revision` itself.
    #
    # The column default is the *widest* set, which is the opposite of what a new
    # account gets (`default_reading_languages`, one language, narrowed in
    # `on_after_register`). Deliberate: a row that reaches the database without going
    # through registration - a migration backfilling accounts that predate this
    # column, a script, a fixture - is one whose owner never chose, and the safe
    # answer for them is the feed they already had rather than a silently narrowed one.
    content_languages: Mapped[list[str]] = mapped_column(
        ARRAY(String(8)), default=lambda: list(settings.CONTENT_LANGUAGES)
    )

    # Object key in the media bucket, not the image itself (see app/core/storage.py;
    # PROFILE_PICTURE_* in app/core/config.py holds the size/type limits enforced on
    # upload, app/api/users.py the upload/delete routes). A *new* key is written on
    # every upload rather than one stable path per user, which is what makes a
    # replaced picture produce a new URL and so invalidate every client cache
    # holding the old one.
    profile_picture_key: Mapped[str | None]

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
    # (see app/core/review_rules.py) so the review-gate check is an O(1) attribute read.
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
    probe_responses: Mapped[list["ProbeResponse"]] = relationship(
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
        """A short-lived presigned URL for this user's profile picture, or None if
        none is set.

        A URL rather than the bytes on UserRead/PostAuthor: a feed lists many posts,
        often several by the same author, and repeating the full image on every one
        would bloat every feed response. It now points straight at the bucket rather
        than back at this backend, so the image never travels through this process
        at all - see app/core/storage.py for how the signature both authorizes the
        fetch and stays byte-stable long enough for the client to cache it.
        """
        if self.profile_picture_key is None:
            return None
        return storage.presigned_url(self.profile_picture_key)

    def __repr__(self):
        return f"User(id={self.id!r}, name={self.email!r})"
