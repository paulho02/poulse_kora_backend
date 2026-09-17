from datetime import datetime, timezone

from fastapi import APIRouter, Depends, File, Form, UploadFile
from sqlalchemy import select
from sqlalchemy.orm import selectinload

from app.core.config import settings
from app.core.errors import api_error
from app.core.logger import get_logger
from app.core.media_validation import ProcessedMedia, process_feedback_upload
from app.core.storage import (
    MEDIA_CACHE_CONTROL,
    StorageError,
    feedback_media_key,
    storage,
)
from app.deps.db import CurrentAsyncSession
from app.deps.locale import CurrentLocale
from app.deps.rate_limit import limit_feedback
from app.deps.users import CurrentSuperuser, OptionalUser
from app.models.feedback import FEEDBACK_KINDS, Feedback, FeedbackMedia
from app.schemas.feedback import FeedbackCreate, FeedbackCreateResult, FeedbackRead

log = get_logger(__name__)

router = APIRouter(prefix="/feedback")

_MIN_RATING = 1
_MAX_RATING = 5


async def feedback_create_form(
    kind: str = Form(...),
    message: str = Form(...),
    rating: int | None = Form(None),
    consent: bool = Form(False),
    is_anonymous: bool = Form(False),
    allow_contact: bool = Form(False),
) -> FeedbackCreate:
    """`POST /feedback` is multipart (it accepts attachments), so its non-file
    fields arrive as form fields rather than a JSON body."""
    return FeedbackCreate(
        kind=kind,
        message=message,
        rating=rating,
        consent=consent,
        is_anonymous=is_anonymous,
        allow_contact=allow_contact,
    )


async def _store_media(
    processed: list[ProcessedMedia],
) -> list[tuple[str, str | None]]:
    """Upload every processed attachment (and its poster frame), returning
    `(object_key, poster_object_key)` in the same order.

    All-or-nothing, exactly like `posts._store_media`: the first failure deletes
    what this call already wrote and raises, so a feedback row never lands
    referencing objects that are not there. Nothing has touched Postgres yet.
    """
    written: list[str] = []
    keys: list[tuple[str, str | None]] = []
    try:
        for item in processed:
            key = feedback_media_key(item.content_type)
            await storage.put_object(
                key,
                item.data,
                content_type=item.content_type,
                cache_control=MEDIA_CACHE_CONTROL,
            )
            written.append(key)

            poster_key = None
            if item.poster and item.poster_content_type:
                poster_key = feedback_media_key(item.poster_content_type)
                await storage.put_object(
                    poster_key,
                    item.poster,
                    content_type=item.poster_content_type,
                    cache_control=MEDIA_CACHE_CONTROL,
                )
                written.append(poster_key)
            keys.append((key, poster_key))
    except StorageError:
        log.exception("feedback.media_upload_failed", discarded_objects=len(written))
        for key in written:
            await storage.delete_object(key)
        raise api_error(503, "media_storage_unavailable") from None
    return keys


@router.post(
    "",
    response_model=FeedbackCreateResult,
    status_code=201,
    dependencies=[Depends(limit_feedback)],
)
async def create_feedback(
    session: CurrentAsyncSession,
    user: OptionalUser,
    locale: CurrentLocale,
    feedback_in: FeedbackCreate = Depends(feedback_create_form),
    files: list[UploadFile] = File(default=[]),
):
    """Submit feedback, a bug report or a feature request, with optional
    screenshots/screen recordings.

    **Works signed out.** The form is linked from the login and register screens as
    well as from the profile, because the reports most worth receiving ("I can't log
    in", "registration rejects my email") are the ones that cannot be filed from
    inside the app. A signed-out submission is anonymous by definition, and the
    route enforces that rather than trusting the form to have sent the right flags:
    `is_anonymous` is forced on and `allow_contact` off when there is no user, so a
    crafted request cannot attach itself to an account it isn't signed in as, and a
    *signed-in* request whose token has expired degrades to an anonymous submission
    rather than being rejected outright — losing the identity, never the report.

    **Consent is a precondition, and its time is stamped here.** Without
    `consent=true` nothing is written at all (`feedback_consent_required`), and the
    accepted moment is taken from the server clock into
    `Feedback.user_agreed_data_saving_at`. A row therefore cannot exist without the
    record of the consent that allowed it, and the timestamp cannot be backdated by
    a client.

    **Choosing anonymity drops the link, not just the display.** `user_id` is left
    NULL rather than stored-and-hidden — see `Feedback` for why that distinction is
    the whole point of the checkbox.

    Attachments go through `process_feedback_upload`, which strips metadata and
    re-encodes like every other upload but does *not* impose the two fixed post
    aspect ratios — a screenshot is whatever shape the screen was. Uploads happen
    before the transaction commits, so a storage failure aborts the submission; the
    residual failure mode is an orphaned object on rollback, which is the cheap one.

    Rate-limited per user, or per client IP when signed out (see
    app/deps/rate_limit.py).
    """
    if not feedback_in.consent:
        raise api_error(400, "feedback_consent_required")
    if feedback_in.kind not in FEEDBACK_KINDS:
        raise api_error(400, "feedback_invalid_kind")

    message = feedback_in.message.strip()
    if not message:
        raise api_error(400, "feedback_message_empty")
    if len(message) > settings.FEEDBACK_MESSAGE_MAX_LENGTH:
        raise api_error(400, "feedback_message_too_long")

    rating = feedback_in.rating
    if rating is not None:
        # Rejected rather than dropped: a rating arriving with a bug report means
        # the client and this route disagree about the form, and silently
        # discarding it would hide that from whoever wrote the client.
        if feedback_in.kind != "feedback":
            raise api_error(400, "feedback_rating_not_allowed")
        if not (_MIN_RATING <= rating <= _MAX_RATING):
            raise api_error(400, "feedback_invalid_rating")

    # Signed out there is no identity to attach and nothing to contact, whatever
    # the form claimed.
    is_anonymous = True if user is None else feedback_in.is_anonymous
    allow_contact = False if user is None else feedback_in.allow_contact
    if is_anonymous and allow_contact:
        # The client hides the contact box behind the anonymous one; a request
        # asking for both is a contradiction, and guessing which half was meant
        # would either leak an address or drop a request to be replied to.
        raise api_error(400, "feedback_contact_requires_identity")

    if len(files) > settings.FEEDBACK_MEDIA_MAX_FILES:
        raise api_error(400, "feedback_media_too_many_files")

    processed: list[ProcessedMedia] = []
    total_media_bytes = 0
    for file in files:
        item = await process_feedback_upload(file)
        total_media_bytes += item.size_bytes
        if total_media_bytes > settings.FEEDBACK_MEDIA_MAX_TOTAL_BYTES:
            raise api_error(400, "feedback_media_total_too_large")
        processed.append(item)

    stored = await _store_media(processed)

    feedback = Feedback(
        kind=feedback_in.kind,
        message=message,
        rating=rating if feedback_in.kind == "feedback" else None,
        # The anonymity rule, in one line: no id is written at all.
        user_id=None if is_anonymous else user.id,
        is_anonymous=is_anonymous,
        # No address is stored alongside it: `Feedback.contact_email` resolves one
        # off the account when a reply is being written, so deleting the account
        # takes the ability to contact with it.
        allow_contact=allow_contact,
        locale=locale,
        user_agreed_data_saving_at=datetime.now(timezone.utc),
    )
    session.add(feedback)
    await session.flush()  # need feedback.id for the media rows

    for item, (object_key, poster_object_key) in zip(processed, stored, strict=True):
        session.add(
            FeedbackMedia(
                feedback_id=feedback.id,
                media_type=item.media_type,
                content_type=item.content_type,
                object_key=object_key,
                size_bytes=item.size_bytes,
                duration_seconds=item.duration_seconds,
                width=item.width,
                height=item.height,
                poster_object_key=poster_object_key,
            )
        )

    await session.commit()
    await session.refresh(feedback)
    # Never the message text: it is user-authored prose that may name anyone, and
    # an anonymous submission deliberately carries no user_id (see Feedback) - so
    # this line records that a report arrived and what shape it is, and the report
    # itself is read through GET /feedback like any other row.
    log.info(
        "feedback.submitted",
        feedback_id=feedback.id,
        kind=feedback.kind,
        anonymous=is_anonymous,
        allow_contact=allow_contact,
        attachments=len(files),
        locale=locale,
        rating=feedback.rating,
    )
    return FeedbackCreateResult(
        id=feedback.id, kind=feedback.kind, created=feedback.created
    )


@router.get("", response_model=list[FeedbackRead])
async def list_feedback(
    session: CurrentAsyncSession,
    user: CurrentSuperuser,
    kind: str | None = None,
    skip: int = 0,
    limit: int = 50,
):
    """Newest-first triage listing. Superuser only — there is no per-user "my
    feedback" view, since a submission may deliberately carry no user to scope it
    to. Attachment URLs are presigned here, which is the only place feedback media
    is ever handed out.
    """
    if kind is not None and kind not in FEEDBACK_KINDS:
        raise api_error(400, "feedback_invalid_kind")

    query = (
        select(Feedback)
        .options(selectinload(Feedback.media))
        .order_by(Feedback.created.desc(), Feedback.id.desc())
        .offset(skip)
        .limit(min(limit, 200))
    )
    if kind is not None:
        query = query.filter(Feedback.kind == kind)
    return list((await session.execute(query)).scalars().all())
