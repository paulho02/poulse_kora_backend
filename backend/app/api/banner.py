from fastapi import APIRouter, status

from app.core import banner as banner_service
from app.deps.locale import CurrentLocale
from app.deps.redis import CurrentRedis
from app.deps.users import CurrentSuperuser
from app.schemas.banner import BannerAdminRead, BannerRead, BannerSet

router = APIRouter(prefix="/banner")


@router.get("", response_model=BannerRead | None)
async def get_banner(redis: CurrentRedis, locale: CurrentLocale):
    """Public, no auth — fetched by the mobile app at start, possibly pre-login.

    Deliberately touches nothing but Redis, same spirit as /health: this is on
    the client's cold-start path and must answer even if the DB is down.
    Resolves to the requester's `Accept-Language` (see app.deps.locale),
    defaulting to `settings.DEFAULT_LOCALE` when absent or unsupported.
    """
    return await banner_service.get_banner(redis, locale)


@router.post("", response_model=BannerAdminRead)
async def set_banner(payload: BannerSet, redis: CurrentRedis, user: CurrentSuperuser):
    """Upsert the banner. Superuser-guarded. `payload.messages` must include
    "en"; other supported locales (e.g. "de") are optional but recommended."""
    return await banner_service.set_banner(redis, payload.messages)


@router.delete("", status_code=status.HTTP_204_NO_CONTENT)
async def clear_banner(redis: CurrentRedis, user: CurrentSuperuser):
    await banner_service.clear_banner(redis)
