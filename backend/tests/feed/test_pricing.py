from redis.asyncio import Redis

from app.core.config import settings
from app.feed import service
from app.feed.pricing import compute_price


class TestPricing:
    def test_min_price_when_queue_empty(self):
        assert compute_price(0, settings) == settings.FEED_PRICE_MIN

    def test_price_rises_one_per_step(self):
        step = settings.FEED_PRICE_STEP_ITEMS
        assert compute_price(step, settings) == settings.FEED_PRICE_MIN + 1
        assert compute_price(step * 2, settings) == settings.FEED_PRICE_MIN + 2

    def test_price_capped_at_max(self):
        assert compute_price(10**9, settings) == settings.FEED_PRICE_MAX


class TestPriceSnapshot:
    async def test_get_price_snapshot_computes_on_first_miss(self, redis: Redis):
        snapshot = await service.get_price_snapshot(redis)
        assert snapshot["price"] == settings.FEED_PRICE_MIN
        assert snapshot["expires_at"] > snapshot["computed_at"]

    async def test_get_price_snapshot_reads_cached_value_without_recomputing(
        self, redis: Redis
    ):
        """The whole point: congestion changing after the snapshot was taken must not
        change what a subsequent read returns, until the snapshot is refreshed."""
        first = await service.get_price_snapshot(redis)

        for i in range(settings.FEED_PRICE_STEP_ITEMS * 3):
            await service.enqueue_operation(redis, post_id=i, channel_id=1)

        second = await service.get_price_snapshot(redis)
        assert second == first

    async def test_refresh_price_snapshot_updates_the_shared_value(self, redis: Redis):
        before = await service.get_price_snapshot(redis)

        for i in range(settings.FEED_PRICE_STEP_ITEMS * 3):
            await service.enqueue_operation(redis, post_id=i, channel_id=1)

        after = await service.refresh_price_snapshot(redis)
        assert after["price"] > before["price"]
        assert (await service.get_price_snapshot(redis)) == after

    async def test_snapshot_expires_at_is_computed_at_plus_refresh_interval(
        self, redis: Redis
    ):
        """The client-facing guarantee window is the refresh interval, not the (longer)
        Redis key TTL — see the FEED_PRICE_TTL_SECONDS vs FEED_PRICE_REFRESH_SECONDS
        split in app/core/config.py."""
        snapshot = await service.get_price_snapshot(redis)
        assert (
            snapshot["expires_at"] - snapshot["computed_at"]
            == settings.FEED_PRICE_REFRESH_SECONDS
        )


class TestMaybeRefreshPriceSnapshot:
    async def test_does_not_overwrite_before_expiry(self, redis: Redis):
        """The bug this guards against: several uncoordinated processes each run
        `run_price_refresher` on their own timer (see app/factory.py). If any of them
        overwrote the snapshot before its quoted `expires_at`, every caller who was
        already shown that expiry (e.g. GET /posts/economy) would have been quoted a
        guarantee window that was cut short — a broken promise, not just a stale
        number."""
        first = await service.get_price_snapshot(redis)

        for i in range(settings.FEED_PRICE_STEP_ITEMS * 3):
            await service.enqueue_operation(redis, post_id=i, channel_id=1)

        # A tick landing well before `first`'s expires_at must not touch it, even
        # though congestion (and therefore the computed price) has since changed.
        still_early = await service.maybe_refresh_price_snapshot(
            redis, now=first["computed_at"] + 1
        )
        assert still_early == first
        assert (await service.get_price_snapshot(redis)) == first

    async def test_refreshes_once_expiry_has_passed(self, redis: Redis):
        first = await service.get_price_snapshot(redis)

        for i in range(settings.FEED_PRICE_STEP_ITEMS * 3):
            await service.enqueue_operation(redis, post_id=i, channel_id=1)

        after_expiry = first["expires_at"] + 1
        refreshed = await service.maybe_refresh_price_snapshot(redis, now=after_expiry)
        assert refreshed["price"] > first["price"]
        assert refreshed["computed_at"] == after_expiry
        assert (await service.get_price_snapshot(redis)) == refreshed
