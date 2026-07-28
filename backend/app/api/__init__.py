from fastapi import APIRouter

from app.api import banner, channels, items, posts, stats, users, utils

api_router = APIRouter()

api_router.include_router(utils.router, tags=["utils"])
api_router.include_router(banner.router, tags=["banner"])
api_router.include_router(users.router, tags=["users"])
api_router.include_router(items.router, tags=["items"])
api_router.include_router(channels.router, tags=["channels"])
api_router.include_router(posts.router, tags=["posts"])
api_router.include_router(stats.router, tags=["stats"])
