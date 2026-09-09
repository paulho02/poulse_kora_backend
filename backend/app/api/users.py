from fastapi import Depends, File, UploadFile, status
from fastapi.routing import APIRouter
from sqlalchemy import func, select
from starlette.responses import Response

from app.core.account_deletion import delete_account
from app.core.errors import api_error
from app.core.logger import get_logger
from app.core.media_validation import process_profile_picture
from app.core.storage import StorageError, profile_picture_key, storage
from app.deps.db import CurrentAsyncSession
from app.deps.rate_limit import limit_password_change
from app.deps.redis import CurrentRedis
from app.deps.users import CurrentSuperuser, CurrentUser, UserManager, get_user_manager
from app.models.user import User
from app.schemas.user import AccountDelete, UserRead

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


@router.delete(
    "/users/me",
    status_code=status.HTTP_204_NO_CONTENT,
    dependencies=[Depends(limit_password_change)],
)
async def delete_own_account(
    body: AccountDelete,
    session: CurrentAsyncSession,
    user: CurrentUser,
    redis: CurrentRedis,
    user_manager: UserManager = Depends(get_user_manager),
):
    """Erase the current account, and its posts too if that is what was chosen.

    Irreversible, and the client is expected to have said so twice before
    calling: the first slide of its dialog is `delete_posts` (and what it costs
    everyone else - the posts stop being readable), the second is the
    confirmation below. What actually happens to each table is
    app/core/account_deletion.py's business; this route is the gate in front of
    it.

    **The current password is required, for the same reason
    `POST /auth/change-password` requires it**: a token is all an attacker who
    has one needs, and destroying an account is at least as bad as taking it
    over. A Google account is the exception rather than a hole - linking
    overwrote its hash with a random value nobody holds, so a password prompt
    there could only ever be refused. Its second slide is the confirmation
    alone.

    Shares the change-password budget rather than getting its own: it is the
    same secret being tested, so a run of guesses must not get a fresh
    allowance by switching routes.

    `CurrentUser`, not `CurrentVerifiedUser` - somebody who never managed to
    verify their address is precisely who is most likely to want the account
    gone, and there is no reason to make them prove it first.
    """
    is_google_account = bool(user.oauth_accounts)
    if not is_google_account:
        if not body.current_password:
            raise api_error(400, "delete_account_password_required")
        verified, _ = user_manager.password_helper.verify_and_update(
            body.current_password, user.hashed_password
        )
        if not verified:
            # WARNING, like the change-password equivalent: this is where a
            # stolen token would be used, and a run of them is what that looks
            # like from here.
            log.warning("account.delete_failed", reason="wrong_password")
            raise api_error(400, "delete_account_wrong_password")

    await delete_account(session, redis, user, delete_posts=body.delete_posts)
