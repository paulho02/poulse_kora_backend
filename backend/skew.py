import asyncio, json, time
from app.core.config import settings
from app.feed import keys, service
from app.redis import redis_client

async def main():
    now = time.time()
    # Give channel 2 a heavy backlog and channel 3 none, then publish a base
    # price high enough that the +-25% band doesn't round away.
    await redis_client.hset(keys.OPS_OUTSTANDING, mapping={"2": "80", "3": "0"})
    await redis_client.set(keys.SUBS_TOTAL, 40)
    for cid in (1, 2, 3, 4, 5):
        await redis_client.sadd(keys.channel(cid), *[f"u{cid}-{i}" for i in range(8)])
        await redis_client.delete(keys.channel_price(cid))
    snap = {
        "price": 12, "computed_at": now,
        "expires_at": now + settings.FEED_PRICE_REFRESH_SECONDS,
        "ops_total": await service.outstanding_ops_total(redis_client),
        "subs_total": await service.subscription_total(redis_client),
    }
    await redis_client.set(keys.PRICE_SNAPSHOT, json.dumps(snap), ex=settings.FEED_PRICE_TTL_SECONDS)
    print("snapshot:", snap)
    print("channel_prices:", await service.channel_prices(redis_client, [1,2,3,4,5]))

asyncio.run(main())
