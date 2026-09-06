from fastapi import File, UploadFile
from fastapi.routing import APIRouter
from sqlalchemy import func, select
from starlette.responses import Response

from app.core.errors import api_error
from app.core.logger import get_logger
from app.core.media_validation import process_profile_picture
from app.core.storage import StorageError, profile_picture_key, storage
from app.deps.db import CurrentAsyncSession
from app.deps.users import CurrentSuperuser, CurrentUser
from app.models.user import User
from app.schemas.user import UserRead

log = get_logger(__name__)

router = APIRouter()


@router.get("/users", response_model=list[UserRead])
async def get_users(
    response: Response,
    session: CurrentAsyncSession,
    user: CurrentSuperuser,
    skip: int = 0,
    limit: int = 100,
):
    total = await session.scalar(select(func.count(User.id)))
    users = (
        (await session.execute(select(User).offset(skip).limit(limit))).scalars().all()
    )
    response.headers["Content-Range"] = f"{skip}-{skip + len(users)}/{total}"
    return users


@router.put("/users/me/profile-picture", response_model=UserRead)
async def upload_profile_picture(
    session: CurrentAsyncSession,
    user: CurrentUser,
    file: UploadFile = File(...),
):
    """Replace the current user's profile picture. The image goes to the media
    bucket (app/core/storage.py) and the user row keeps only its key; see
    PROFILE_PICTURE_* in app/core/config.py for the limits enforced here.

    The bytes are validated and re-encoded first (`process_profile_picture`), so
    what lands in the bucket is a real image under a `Content-Type` this process
    derived rather than one the client asserted, with its EXIF - GPS included -
    dropped. That matters more here than it did when these bytes lived in Postgres
    behind an authenticated route: the object is now served straight from the
    bucket to whoever holds the presigned URL.

    Order matters: upload first, commit second, delete the old object last. A
    failed upload therefore leaves the previous picture intact, and a crash between
    the two writes at worst orphans an object in the bucket - never leaves a user
    row pointing at something that isn't there.
    """
    data, content_type = await process_profile_picture(file)

    previous_key = user.profile_picture_key
    key = profile_picture_key(user.id, content_type)
    try:
        await storage.put_object(key, data, content_type=content_type)
    except StorageError:
        log.exception("user.profile_picture_upload_failed", bytes=len(data))
        raise api_error(503, "media_storage_unavailable") from None

    user.profile_picture_key = key
    session.add(user)
    await session.commit()

    if previous_key:
        await storage.delete_object(previous_key)
    log.info(
        "user.profile_picture_updated",
        bytes=len(data),
        content_type=content_type,
        replaced=bool(previous_key),
    )
    return user


@router.delete("/users/me/profile-picture", response_model=UserRead)
async def delete_profile_picture(session: CurrentAsyncSession, user: CurrentUser):
    previous_key = user.profile_picture_key
    user.profile_picture_key = None
    session.add(user)
    await session.commit()
    if previous_key:
        await storage.delete_object(previous_key)
    log.info("user.profile_picture_removed", had_picture=bool(previous_key))
    return user
