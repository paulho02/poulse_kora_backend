import asyncio

import pytest
from redis.asyncio import Redis

from app.core.config import settings
from app.feed import keys, service
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


class TestRunPriceRefresher:
    async def test_runs_immediately_and_stops_cleanly_on_cancel(self, redis: Redis):
        """Runs once before the first sleep (see the docstring) — a fresh deploy
        must not leave the snapshot missing for a full FEED_PRICE_REFRESH_SECONDS."""
        assert not await redis.exists(keys.PRICE_SNAPSHOT)

        task = asyncio.create_task(service.run_price_refresher(redis))
        try:
            for _ in range(50):
                if await redis.exists(keys.PRICE_SNAPSHOT):
                    break
                await asyncio.sleep(0.02)
            else:
                pytest.fail("run_price_refresher never published a snapshot")
        finally:
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task

    async def test_survives_a_transient_error_and_keeps_looping(
        self, redis: Redis, monkeypatch
    ):
        calls = {"n": 0}
        real = service.maybe_refresh_price_snapshot

        async def flaky(*args, **kwargs):
            calls["n"] += 1
            if calls["n"] == 1:
                raise RuntimeError("transient blip")
            return await real(*args, **kwargs)

        monkeypatch.setattr(service, "maybe_refresh_price_snapshot", flaky)
        monkeypatch.setattr(settings, "FEED_PRICE_REFRESH_SECONDS", 0.02)

        task = asyncio.create_task(service.run_price_refresher(redis))
        try:
            for _ in range(50):
                if calls["n"] >= 2:
                    break
                await asyncio.sleep(0.02)
            else:
                pytest.fail("run_price_refresher did not continue past the error")
        finally:
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task

    async def test_cancellation_during_the_refresh_call_itself_propagates(
        self, redis: Redis, monkeypatch
    ):
        """Cancellation must never be treated as "just another exception to log and
        continue" - confirmed here specifically for the window inside the refresh
        call itself, not just the (much larger, easier to hit by accident) sleep."""
        started = asyncio.Event()

        async def hang_forever(*args, **kwargs):
            started.set()
            await asyncio.Event().wait()

        monkeypatch.setattr(service, "maybe_refresh_price_snapshot", hang_forever)

        task = asyncio.create_task(service.run_price_refresher(redis))
        await asyncio.wait_for(started.wait(), timeout=2.0)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
