"""The observed price range.

There is no single admission price any more - every (channel, language) route prices
itself - so the client shows a range. Answering "what is the cheapest route right now"
*exactly* would mean pricing every route on every window tick, which needs the price
refresher to enumerate channels out of Postgres. Instead the range is observed: every
route price computed for a real request widens it, and `GET /channels` prices every
route of the channels it was already listing. These tests pin the two properties that
buys - it converges from ordinary traffic, and a window that has observed nothing of
its own carries the previous one's spread across rather than reporting a flat range.
"""

import json
import time
import uuid

from httpx import AsyncClient
from redis.asyncio import Redis

from app.core.config import settings
from app.feed import keys, service
from app.models.user import User
from tests.utils import get_jwt_header


async def _publish_snapshot(
    redis: Redis, price: int, ops_total: int, subs_total: int, now: float | None = None
) -> dict:
    if now is None:
        now = time.time()
    snapshot = {
        "price": price,
        "computed_at": now,
        "expires_at": now + settings.FEED_PRICE_REFRESH_SECONDS,
        "ops_total": ops_total,
        "subs_total": subs_total,
    }
    await redis.set(
        keys.PRICE_SNAPSHOT, json.dumps(snapshot), ex=settings.FEED_PRICE_TTL_SECONDS
    )
    return snapshot


async def _give_audience(redis: Redis, channel_id: int, language: str, count: int):
    await redis.sadd(
        keys.audience(channel_id, language), *[str(uuid.uuid4()) for _ in range(count)]
    )


class TestObservedRange:
    async def test_nothing_observed_yet_reads_as_none(self, redis: Redis):
        """A real answer callers must handle: right after a window rolls over there is
        nothing to report, and the honest fallback is the base price at both ends."""
        await redis.delete(keys.PRICE_RANGE)
        await _publish_snapshot(redis, price=10, ops_total=100, subs_total=1000)

        assert await service.read_price_range(redis) is None

    async def test_widens_as_routes_are_priced(self, redis: Redis):
        await redis.delete(keys.PRICE_RANGE)
        await _publish_snapshot(redis, price=20, ops_total=100, subs_total=1000)
        # Same audience, opposite backlogs: the two ends of the band.
        await _give_audience(redis, 1401, "en", 100)
        await _give_audience(redis, 1402, "en", 100)
        await redis.hset(keys.OPS_OUTSTANDING, keys.route(1401, "en"), 50)

        prices = await service.route_prices(redis, [(1401, "en"), (1402, "en")])

        low, high = await service.read_price_range(redis)
        assert low == min(prices.values())
        assert high == max(prices.values())
        assert low < high

    async def test_is_scoped_per_channel(self, redis: Redis):
        """A channel's own tag must bracket its own routes only - the whole point of
        showing one next to the global range."""
        await redis.delete(
            keys.PRICE_RANGE,
            keys.channel_price_range(1411),
            keys.channel_price_range(1412),
        )
        await _publish_snapshot(redis, price=20, ops_total=100, subs_total=1000)
        await _give_audience(redis, 1411, "en", 100)
        await _give_audience(redis, 1412, "en", 100)
        # 1412 is heavily congested, 1411 carries no backlog at all.
        await redis.hset(keys.OPS_OUTSTANDING, keys.route(1412, "en"), 10_000)

        prices = await service.route_prices(redis, [(1411, "en"), (1412, "en")])
        cheap, dear = prices[(1411, "en")], prices[(1412, "en")]
        assert cheap < dear

        assert await service.read_price_range(redis, channel_id=1411) == (cheap, cheap)
        assert await service.read_price_range(redis, channel_id=1412) == (dear, dear)
        # The global range spans both.
        assert await service.read_price_range(redis) == (cheap, dear)

    async def test_an_older_window_is_rescaled_onto_the_current_base(
        self, redis: Redis
    ):
        """A previous window's numbers must never be reported as current - they were
        measured against a base price that has since moved - but they are still the best
        evidence there is about the *shape* of the spread, so they are carried over
        scaled by how far the base moved.

        The bug this fixes: reporting the base price at both ends whenever a window had
        observed nothing yet made the composer's range collapse to a single number about
        once a minute, and quote one lower than what `create_post` would charge.
        """
        now = time.time()
        await redis.delete(keys.PRICE_RANGE)
        await _publish_snapshot(
            redis, price=20, ops_total=100, subs_total=1000, now=now
        )
        # Two ends of the band, so there is a real spread to carry.
        await _give_audience(redis, 1421, "en", 100)
        await _give_audience(redis, 1422, "en", 100)
        await redis.hset(keys.OPS_OUTSTANDING, keys.route(1421, "en"), 50)
        prices = await service.route_prices(
            redis, [(1421, "en"), (1422, "en")], now=now
        )
        observed = await service.read_price_range(redis)
        assert observed == (min(prices.values()), max(prices.values()))

        later = now + settings.FEED_PRICE_REFRESH_SECONDS + 1
        await _publish_snapshot(
            redis, price=10, ops_total=100, subs_total=1000, now=later
        )

        carried = await service.read_price_range(redis)
        assert carried is not None
        low, high = carried
        # Halving the base price halves the spread it was observed against, and the new
        # base is inside the answer either way: a route with neutral congestion is
        # charged exactly that.
        assert (low, high) == (round(observed[0] / 2), max(round(observed[1] / 2), 10))
        assert low <= 10 <= high
        # Still a range, which is the whole point - the flat fallback was not.
        assert low < high

    async def test_a_spread_with_no_recorded_base_is_not_carried(self, redis: Redis):
        """Written before the base price was recorded alongside it. There is then
        nothing to say what the numbers were relative to, so that one window degrades to
        the base-price fallback rather than guessing."""
        now = time.time()
        await redis.delete(keys.PRICE_RANGE)
        await _publish_snapshot(
            redis, price=20, ops_total=100, subs_total=1000, now=now
        )
        await _give_audience(redis, 1431, "en", 100)
        await service.route_prices(redis, [(1431, "en")], now=now)
        await redis.hdel(keys.PRICE_RANGE, "base")

        later = now + settings.FEED_PRICE_REFRESH_SECONDS + 1
        await _publish_snapshot(
            redis, price=8, ops_total=100, subs_total=1000, now=later
        )

        assert await service.read_price_range(redis) is None


class TestEconomyEndpoint:
    async def test_reports_the_range_around_the_base_price(
        self, client: AsyncClient, redis: Redis, create_user, create_channel
    ):
        user: User = await create_user()
        await create_channel()
        # `GET /channels` prices every route of every channel it lists, which is what
        # makes the range converge on the first channel-list load of each window.
        await client.get(settings.API_PATH + "/channels", headers=get_jwt_header(user))

        resp = await client.get(
            settings.API_PATH + "/posts/economy", headers=get_jwt_header(user)
        )

        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["post_price_min"] <= body["post_price_max"]
        assert settings.FEED_PRICE_MIN <= body["post_price_min"]
        assert body["post_price_max"] <= settings.FEED_PRICE_MAX

    async def test_falls_back_to_the_base_price_with_nothing_observed(
        self, client: AsyncClient, redis: Redis, create_user
    ):
        user: User = await create_user()
        await redis.delete(keys.PRICE_RANGE)

        resp = await client.get(
            settings.API_PATH + "/posts/economy", headers=get_jwt_header(user)
        )

        body = resp.json()
        assert body["post_price_min"] == body["post_price"]
        assert body["post_price_max"] == body["post_price"]
