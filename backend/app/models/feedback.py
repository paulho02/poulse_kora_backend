from datetime import datetime
from typing import TYPE_CHECKING
from uuid import UUID

from fastapi_users_db_sqlalchemy import GUID
from sqlalchemy.orm import Mapped, mapped_column, relationship
from sqlalchemy.sql.functions import func
from sqlalchemy.sql.schema import ForeignKey, Index
from sqlalchemy.sql.sqltypes import DateTime, String, Text

from app.core.storage import storage
from app.db import Base

if TYPE_CHECKING:
    from app.models.user import User

# What the submission is about. `rating` only applies to "feedback" - see the
# column's comment.
FEEDBACK_KINDS = ("feedback", "bug", "feature_request", "other")

# Triage state, ours rather than the submitter's - see `Feedback.status`. Ordered
# as the workflow runs, though nothing enforces that ordering yet.
FEEDBACK_STATUSES = ("new", "viewed", "noted", "done")

FEEDBACK_STATUS_DEFAULT = "new"


class Feedback(Base):
    """One submission from the in-app feedback form (app/api/feedback.py).

    Reachable **signed out** as well as in - it is linked from the login/register
    screen, since "I can't get in" is precisely the report that cannot be filed from
    inside the app. That single fact drives most of what follows.

    **Anonymity is the absence of the link, not a flag on it.** When
    `is_anonymous` is set, `user_id` is left NULL rather than being written and
    hidden behind a boolean: a row that still carried the id would be de-anonymized
    by anyone with database access, which is not what someone ticking "send
    anonymously" is agreeing to. A signed-out submission is anonymous for the
    simpler reason that there is no id to store. The flag is kept anyway, so a NULL
    `user_id` can be read as "chose anonymity" rather than as missing data - and so
    a future signed-in-but-not-anonymous default cannot be confused with one.

    **`user_agreed_data_saving_at` is the compliance record**, and the reason this
    table can hold free text and media at all. It is stamped from the *server* clock
    at the moment the submission is accepted - never taken from the client, which
    could claim any time it likes - and the route refuses the submission outright
    unless consent was given, so the column is NOT NULL by construction: a row
    existing is itself the evidence that consent preceded it. Attachments are
    covered by the same instant, since they arrive in the same request.

    **There is no stored contact address** - `allow_contact` is consent to be
    reached *as an account*, and the address is resolved live off `user` (see the
    `contact_email` property). Snapshotting the address instead would survive the
    thing that should destroy it: `user_id` is `ON DELETE SET NULL`, so erasing an
    account already cuts the link, whereas a copied address would sit on in this
    table after the person asked to be forgotten. Resolving it live also means a
    reply never goes to an address the user has since changed away from.
    """

    __tablename__ = "feedback"
    __table_args__ = (
        # Triage reads this table newest-first, optionally narrowed to one kind
        # (all the open bug reports), which is exactly this index.
        Index("ix_feedback_kind_created", "kind", "created"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)

    kind: Mapped[str] = mapped_column(String(20))  # one of FEEDBACK_KINDS
    message: Mapped[str] = mapped_column(Text)
    # 1-5, and only meaningful for kind == "feedback" - the form shows the stars for
    # that kind alone, and the route rejects a rating sent with any other kind
    # rather than silently dropping it. Optional even then: someone can write about
    # the app without scoring it.
    rating: Mapped[int | None]

    # NULL whenever `is_anonymous` is true, and whenever the submission arrived
    # signed out. `ondelete` is deliberately not set to CASCADE: deleting an account
    # should not silently destroy the bug reports it filed, and SET NULL is the
    # right shape here since a NULL id is already a meaningful state in this table.
    user_id: Mapped[UUID | None] = mapped_column(
        GUID, ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    is_anonymous: Mapped[bool] = mapped_column(default=False, server_default="false")

    # Consent to be contacted *as an account*: the address itself is never stored,
    # only resolved through `user` when a reply is actually being written. Can only
    # be true alongside a non-NULL `user_id` - the route refuses the other
    # combination, since there would be nothing to resolve.
    allow_contact: Mapped[bool] = mapped_column(default=False, server_default="false")

    # Which language the form was submitted in (resolved from Accept-Language, see
    # app/core/locale.py). Not derivable from `user` and not droppable in favour of
    # it: there is deliberately no `User.locale` column in this codebase (the
    # pre-login banner has no user to read one from), and the rows that most need
    # this are the anonymous ones, which have no user at all. Someone answering a
    # report has to know which language to write back in, and free text in this
    # table is not necessarily English.
    locale: Mapped[str | None] = mapped_column(String(10), nullable=True)

    # See the class docstring. Server-stamped, never client-supplied.
    user_agreed_data_saving_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True)
    )

    # Where this sits in triage: one of FEEDBACK_STATUSES, "new" on arrival.
    #
    # **Internal, and not part of the submission.** A submitter has no say in it -
    # not because it is filtered out, but because there is nowhere to say it: the
    # create route's form is an explicit allow-list (`feedback_create_form`) and
    # builds the row itself, so this is unreachable from a request in exactly the
    # way `user_agreed_data_saving_at` and `user_id` are.
    #
    # Deliberately just a column for now: nothing reads or writes it yet, there is
    # no transition rule, and no route sets it. Kept as a plain string rather than a
    # DB enum so adding a state later is a value, not a migration - matching `kind`
    # and every other short label in this codebase, whose wire values are lowercase.
    # No index either, until there is a query that actually filters on it; the
    # existing (kind, created) index is what the listing currently uses.
    status: Mapped[str] = mapped_column(
        String(20),
        default=FEEDBACK_STATUS_DEFAULT,
        server_default=FEEDBACK_STATUS_DEFAULT,
    )

    created: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )

    # Eager-loaded for the same reason as `User.oauth_accounts`: `contact_email`
    # below is a property read during serialization, and a lazy attribute access
    # there is a hard error under async SQLAlchemy. `selectin` costs one extra
    # indexed lookup; unlike a joined eager load it carries no `.unique()`
    # obligation on every `select(Feedback)` somebody later writes.
    user: Mapped["User | None"] = relationship(lazy="selectin")
    media: Mapped[list["FeedbackMedia"]] = relationship(
        back_populates="feedback", cascade="all, delete", order_by="FeedbackMedia.id"
    )

    @property
    def contact_email(self) -> str | None:
        """Where to reply, or None when there is nobody to reply to.

        Read off the account rather than off a stored copy - see the class
        docstring. Returns None for an anonymous submission (no `user`), for one
        that never asked to be contacted, and for one whose account has since been
        deleted (`user_id` is nulled, so the link is simply gone). All three mean
        the same thing to whoever is reading the report: do not write back.
        """
        if not self.allow_contact or self.user is None:
            return None
        return self.user.email


class FeedbackMedia(Base):
    """One screenshot or screen recording attached to a `Feedback` row.

    Deliberately its own table rather than a reuse of `PostMedia`: the two share a
    shape but not a lifetime or an access rule, and post media is reached through
    `PostBlock` positions that mean nothing here (a feedback attachment has no
    display order to speak of beyond the order it was picked in).

    The bytes live in the media bucket like everything else, under their own
    `feedback-media/` prefix and a random key that encodes nothing about the
    submitter - which for this table is not a nicety, since the row it belongs to
    may have no submitter recorded at all.

    Unlike post media, these are **never served to another user**: the only route
    that hands out URLs for them is the superuser-only listing.
    """

    __tablename__ = "feedback_media"
    __table_args__ = (Index("ix_feedback_media_feedback_id", "feedback_id"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    feedback_id: Mapped[int] = mapped_column(ForeignKey("feedback.id"))

    media_type: Mapped[str] = mapped_column(String(10))  # "image" | "video"
    # Derived from what Pillow/ffprobe actually parsed, not from the client's
    # header - see app/core/media_validation.py.
    content_type: Mapped[str]
    object_key: Mapped[str]
    size_bytes: Mapped[int]
    duration_seconds: Mapped[float | None]
    width: Mapped[int | None]
    height: Mapped[int | None]
    # Video only, and non-fatal to extract, same as PostMedia.poster_object_key.
    poster_object_key: Mapped[str | None]

    created: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )

    feedback: Mapped["Feedback"] = relationship(back_populates="media")

    @property
    def url(self) -> str:
        return storage.presigned_url(self.object_key)

    @property
    def poster_url(self) -> str | None:
        if self.poster_object_key is None:
            return None
        return storage.presigned_url(self.poster_object_key)
