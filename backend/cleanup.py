import asyncio
from app.feed import keys
from app.redis import redis_client

async def main():
    # Drop the fake subscribers/backlog this session injected, plus the derived
    # price caches; rebuild_from_pg then restores the real membership from PG.
    await redis_client.delete(keys.OPS_OUTSTANDING, keys.SUBS_TOTAL, keys.PRICE_SNAPSHOT)
    for cid in range(1, 20):
        await redis_client.delete(keys.channel(cid), keys.channel_price(cid))
    print("cleared skewed keys")

asyncio.run(main())
