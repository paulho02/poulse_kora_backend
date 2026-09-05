import asyncio, json
from fastapi_users.jwt import generate_jwt
from sqlalchemy import select
from app.db import async_session_maker
from app.deps.users import get_jwt_strategy
from app.models.user import User
from app.models.channel import Channel
from app.redis import redis_client
from app.feed import service

async def main():
    async with async_session_maker() as s:
        user = (await s.execute(select(User).limit(1))).scalars().first()
        channels = (await s.execute(select(Channel).limit(5))).scalars().all()
    st = get_jwt_strategy()
    token = generate_jwt({"sub": str(user.id), "aud": st.token_audience}, st.secret, st.lifetime_seconds)
    print("TOKEN:" + token)
    print("channels in db:", [(c.id, c.name) for c in channels])
    snap = await service.get_price_snapshot(redis_client)
    print("snapshot:", json.dumps(snap))
    prices = await service.channel_prices(redis_client, [c.id for c in channels])
    print("channel_prices:", prices)
    print("subs_total:", await service.subscription_total(redis_client))
    print("ops_total:", await service.outstanding_ops_total(redis_client))

asyncio.run(main())
