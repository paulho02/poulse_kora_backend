import uuid
from datetime import datetime

from pydantic import BaseModel, ConfigDict


class FeedbackCreate(BaseModel):
    """The non-file half of a feedback submission.

    `POST /feedback` is multipart (it accepts attachments), so these arrive as form
    fields rather than a JSON body - see `feedback_create_form` in
    app/api/feedback.py, which is where the values are parsed and where the rules
    *between* them (a rating only with kind "feedback", contact only when not
    anonymous) are enforced. Nothing here is trusted on its own: an unauthenticated
    submission has `is_anonymous` forced on regardless of what was sent.
    """

    kind: str
    message: str
    rating: int | None = None
    # Must be true, or the submission is refused - see the route. The *time* it was
    # given is stamped server-side (`Feedback.user_agreed_data_saving_at`); the
    # client only says that it was.
    consent: bool = False
    is_anonymous: bool = False
    allow_contact: bool = False


class FeedbackCreateResult(BaseModel):
    """What the client gets back: enough to confirm the submission landed, and
    nothing more. The form is fire-and-forget - there is no screen that reads a
    submission back - so echoing the message and its attachments would only hand a
    shared device a way to re-read what was just sent anonymously."""

    id: int
    kind: str
    created: datetime


class FeedbackMediaRead(BaseModel):
    """One attachment's metadata plus presigned URLs for its bytes. Only ever
    serialized into the superuser listing."""

    id: int
    media_type: str
    content_type: str
    url: str
    duration_seconds: float | None
    width: int | None
    height: int | None
    poster_url: str | None

    model_config = ConfigDict(from_attributes=True)


class FeedbackRead(BaseModel):
    """A submission as triage sees it (superuser only).

    `user_id` is null for an anonymous submission because the row genuinely does
    not carry one - see `Feedback` - not because this schema hides it.
    `contact_email` is not a column at all: it is resolved off the linked account
    at serialization time, and is null whenever there is nobody to write back to
    (anonymous, contact not agreed to, or the account since deleted).
    """

    id: int
    kind: str
    # Triage state (see FEEDBACK_STATUSES). Read-only here and everywhere else for
    # now - there is no route that sets it yet.
    status: str
    message: str
    rating: int | None
    is_anonymous: bool
    user_id: uuid.UUID | None
    allow_contact: bool
    contact_email: str | None
    locale: str | None
    user_agreed_data_saving_at: datetime
    created: datetime
    media: list[FeedbackMediaRead]

    model_config = ConfigDict(from_attributes=True)
