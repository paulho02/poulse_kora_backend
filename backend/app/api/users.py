import uuid

from fastapi import File, UploadFile
from fastapi.routing import APIRouter
from sqlalchemy import func, select
from starlette.responses import Response

from app.core.config import settings
from app.core.errors import api_error
from app.deps.db import CurrentAsyncSession
from app.deps.users import CurrentSuperuser, CurrentUser
from app.models.user import User
from app.schemas.user import UserRead

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
    """Replace the current user's profile picture. Stored directly as bytes on the
    user row - there is no file storage yet, see PROFILE_PICTURE_* in
    app/core/config.py for the limits enforced here.
    """
    if file.content_type not in settings.PROFILE_PICTURE_ALLOWED_CONTENT_TYPES:
        raise api_error(400, "profile_picture_invalid_type")

    data = await file.read()
    if len(data) > settings.PROFILE_PICTURE_MAX_BYTES:
        raise api_error(400, "profile_picture_too_large")

    user.profile_picture = data
    user.profile_picture_content_type = file.content_type
    session.add(user)
    await session.commit()
    return user


@router.delete("/users/me/profile-picture", response_model=UserRead)
async def delete_profile_picture(session: CurrentAsyncSession, user: CurrentUser):
    user.profile_picture = None
    user.profile_picture_content_type = None
    session.add(user)
    await session.commit()
    return user


@router.get("/users/{user_id}/profile-picture")
async def get_profile_picture(
    user_id: uuid.UUID, session: CurrentAsyncSession, viewer: CurrentUser
):
    """Raw image bytes for a profile picture, referenced by `profile_picture_url` on
    UserRead/PostAuthor. A separate binary endpoint rather than embedding base64 in
    those JSON bodies - see User.profile_picture_url for why.
    """
    target = await session.get(User, user_id)
    if target is None or target.profile_picture is None:
        raise api_error(404, "profile_picture_not_found")
    return Response(
        content=target.profile_picture,
        media_type=target.profile_picture_content_type or "application/octet-stream",
    )
