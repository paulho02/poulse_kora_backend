from typing import Annotated

from fastapi import Depends
from redis.asyncio import Redis

from app.redis import redis_client


async def get_redis() -> Redis:
    return redis_client


CurrentRedis = Annotated[Redis, Depends(get_redis)]
