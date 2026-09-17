from fastapi import Depends, File, UploadFile, status
from fastapi.routing import APIRouter
from sqlalchemy import func, select
from starlette.responses import Response, StreamingResponse

from app.core import account_export, languages
from app.core.account_deletion import delete_account
from app.core.errors import api_error
from app.core.logger import get_logger
from app.core.media_validation import process_profile_picture
from app.core.storage import (
    MEDIA_CACHE_CONTROL,
    StorageError,
    profile_picture_key,
    storage,
)
from app.deps.db import CurrentAsyncSession
from app.deps.locale import CurrentLocale
from app.deps.rate_limit import limit_account_export, limit_password_change
from app.deps.redis import CurrentRedis
from app.deps.users import CurrentSuperuser, CurrentUser, UserManager, get_user_manager
from app.feed import service
from app.models.channel_subscription import ChannelSubscription
from app.models.user import User
from app.schemas.user import AccountDelete, ContentLanguagesUpdate, UserRead

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
        await storage.put_object(
            key,
            data,
            content_type=content_type,
            # The same header every other upload path sets, and the reason this
            # one is spelled out rather than left to a default: it was the one
            # path that omitted it, so a profile picture was re-downloaded every
            # time its presigned URL rolled over (~15 minutes) while post media
            # was cached for a day. See MEDIA_CACHE_CONTROL for why it is safe
            # to be this aggressive - a key is a fresh UUID per upload and is
            # never rewritten, so a cached copy cannot go stale.
            cache_control=MEDIA_CACHE_CONTROL,
        )
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


@router.put("/users/me/content-languages", response_model=UserRead)
async def set_content_languages(
    payload: ContentLanguagesUpdate,
    session: CurrentAsyncSession,
    user: CurrentUser,
    redis: CurrentRedis,
):
    """Set which languages this reader accepts posts in.

    Its own route rather than a field on `PATCH /users/me`, because the column is only
    half the change: the other half is rewriting one Redis audience membership per
    (subscribed channel x language), which is what fan-out actually samples. The
    fastapi-users update router would write the column and know nothing about the rest,
    leaving the database saying one thing and delivery doing another.

    Order is Postgres first, Redis second, and the residual failure is deliberate: a
    commit followed by a failed sync leaves memberships that a re-run of this route -
    or `rebuild_from_pg` - corrects, because `sync_content_languages` writes the
    absolute set rather than a diff. The reverse order could leave Redis delivering by
    a preference the database never recorded, which nothing would ever reconcile.

    Idempotent. Re-sending the same set is a no-op down to the `settings_revision`
    bump, which is skipped when the canonical form is unchanged - so a client that
    re-asserts its preference on every launch does not manufacture a settings conflict
    for the user's other devices.
    """
    requested = payload.languages
    if not requested:
        # An empty set is an audience of nowhere: no route would ever select this
        # reader, and their feed would be permanently empty with nothing to explain it.
        raise api_error(400, "content_languages_empty")
    unknown = [code for code in requested if code not in languages.reading_languages()]
    if unknown:
        # Refused rather than filtered. A client sending a language this deployment
        # does not have is out of date or wrong, and silently storing the subset would
        # leave it believing a preference the server never accepted.
        raise api_error(400, "content_languages_invalid")

    cleaned = languages.sanitize_reading_languages(requested)
    changed = cleaned != user.content_languages
    if changed:
        user.content_languages = cleaned
        # Bumped here rather than by UserManager._update, which this route bypasses.
        # It is a settings change like any other: another device holding a stale value
        # has to be able to tell that someone else moved it.
        user.settings_revision += 1
        await session.commit()

    channel_ids = (
        (
            await session.execute(
                select(ChannelSubscription.channel_id).filter(
                    ChannelSubscription.user_id == user.id
                )
            )
        )
        .scalars()
        .all()
    )
    # Run even when nothing changed: this is the one route that reconciles a reader's
    # memberships, so a client re-asserting its preference is also the cheapest repair
    # for a sync that failed half-way last time.
    await service.sync_content_languages(
        redis, str(user.id), list(channel_ids), cleaned
    )
    log.info(
        "user.content_languages_set",
        languages=cleaned,
        channels=len(channel_ids),
        changed=changed,
    )
    return user


@router.get(
    "/users/me/export",
    dependencies=[Depends(limit_account_export)],
    response_class=StreamingResponse,
    responses={
        200: {
            "content": {"application/zip": {}},
            "description": (
                "A ZIP archive: README.txt, data.json and every file this "
                "account uploaded."
            ),
        }
    },
)
async def export_own_data(
    session: CurrentAsyncSession,
    user: CurrentUser,
    redis: CurrentRedis,
    locale: CurrentLocale,
):
    """Download everything this service holds about the current account.

    The automated answer to GDPR Art. 15 (access) and Art. 20 (portability),
    replacing the documented-manual-process-within-a-month that would otherwise
    be the minimum. What goes in the archive, and why it is an archive rather
    than a JSON body full of media links, is app/core/account_export.py's
    business; this route is the gate in front of it.

    **Everything is read here, in the handler, and nothing during the stream.**
    FastAPI closes a `yield` dependency when the handler returns - so by the time
    a `StreamingResponse` body is being consumed, `session` is closed and its
    connection is back in the pool. `collect` therefore returns plain Python and
    the generator touches only the storage client, which lives as long as the
    process. Doing it the other way round works in a test and fails in
    production, which is the worst available failure mode.

    **No password, unlike `DELETE /users/me`.** The two are not symmetrical: the
    deletion route asks for one because a stolen token would otherwise be enough
    to destroy an account, and there is nothing this one can destroy. A stolen
    token can already read every one of these records through the ordinary API,
    one route at a time - what this adds is convenience, not reach - and putting
    a password prompt in front of a legal right would refuse it outright to a
    Google account, which has no password to give.

    `CurrentUser`, not `CurrentVerifiedUser`, for the same reason the deletion
    route uses it: the right of access does not wait on an email link.
    """
    export = await account_export.collect(session, redis, user, locale=locale)
    return StreamingResponse(
        account_export.stream_zip(export),
        media_type="application/zip",
        headers={
            "Content-Disposition": f'attachment; filename="{export.filename}"',
            # No `Content-Length`: the archive is built as it is sent, so its
            # size is not known until it is finished. And `no-store`, because
            # this is the one response that concentrates an entire account's
            # personal data into a single file - it has no business sitting in
            # an intermediary's cache or a browser's disk cache afterwards.
            "Cache-Control": "no-store",
        },
    )


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
