"""Print the live pricing state, plus a JWT for calling the price routes by hand.

What the pricing snapshot actually contains is otherwise only visible through
`GET /posts/economy` and `GET /channels`, both of which need a bearer token — so
checking whether a change to `channel_factor` did what was intended meant registering
a user, verifying the address and logging in first. This prints the raw state
(snapshot, per-channel prices, and the two counters the factor divides) and mints a
token for the first user in the database so the routes can be curl'd immediately.

**Never run this against production.** It reads nothing sensitive, but it *forges a
valid session token* for a real account without that account's password — the JWT is
signed with the deployment's own `SECRET_KEY`, so it is indistinguishable from one
issued by a real login. On a shared or production database that is an authentication
bypass, not a diagnostic.

Usage (inside the backend container):
    docker compose exec backend python -m scripts.dangerous.pricecheck
"""

import asyncio
import json

from fastapi_users.jwt import generate_jwt
from sqlalchemy import select

from app.db import async_session_maker
from app.deps.users import get_jwt_strategy
from app.feed import service
from app.models.channel import Channel
from app.models.user import User
from app.redis import redis_client


async def main():
    async with async_session_maker() as s:
        user = (await s.execute(select(User).limit(1))).scalars().first()
        channels = (await s.execute(select(Channel).limit(5))).scalars().all()
    st = get_jwt_strategy()
    token = generate_jwt(
        {"sub": str(user.id), "aud": st.token_audience},
        st.secret,
        st.lifetime_seconds,
    )
    print("TOKEN:" + token)
    print("channels in db:", [(c.id, c.name) for c in channels])
    snap = await service.get_price_snapshot(redis_client)
    print("snapshot:", json.dumps(snap))
    prices = await service.channel_prices(redis_client, [c.id for c in channels])
    print("channel_prices:", prices)
    print("subs_total:", await service.subscription_total(redis_client))
    print("ops_total:", await service.outstanding_ops_total(redis_client))


asyncio.run(main())
