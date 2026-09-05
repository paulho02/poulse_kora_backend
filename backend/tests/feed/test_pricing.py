import asyncio
import json
import time
import uuid

import pytest
from redis.asyncio import Redis

from app.core.config import settings
from app.feed import keys, service
from app.feed.pricing import (
    channel_factor,
    channel_price,
    compute_price,
    price_target,
)


class TestPriceTarget:
    def test_floor_wins_when_active_users_is_low(self):
        assert price_target(0, settings) == settings.FEED_PRICE_TARGET_MIN_ITEMS
        assert price_target(5, settings) == settings.FEED_PRICE_TARGET_MIN_ITEMS

    def test_ratio_wins_once_active_users_is_large(self):
        active_users = settings.FEED_PRICE_TARGET_MIN_ITEMS * 100
        expected = round(settings.FEED_PRICE_BUFFER_RATIO * active_users)
        assert price_target(active_users, settings) == expected
        assert expected > settings.FEED_PRICE_TARGET_MIN_ITEMS


class TestComputePrice:
    def test_within_deadband_leaves_price_unchanged(self):
        target = price_target(0, settings)
        assert compute_price(3, target, 0, settings) == 3

    def test_over_target_steps_up_by_exactly_one(self):
        target = price_target(0, settings)
        far_over = target * 10 + 100
        assert compute_price(3, far_over, 0, settings) == 4

    def test_under_target_steps_down_by_exactly_one(self):
        target = price_target(0, settings)
        assert compute_price(3, 0, 0, settings) == 2
        assert target > 0  # sanity: 0 is genuinely far under any target

    def test_price_never_drops_below_min(self):
        assert compute_price(settings.FEED_PRICE_MIN, 0, 0, settings) == (
            settings.FEED_PRICE_MIN
        )

    def test_price_never_rises_above_max(self):
        far_over = price_target(0, settings) * 10 + 100
        assert compute_price(
            settings.FEED_PRICE_MAX, far_over, 0, settings
        ) == settings.FEED_PRICE_MAX

    def test_a_single_tick_never_moves_price_by_more_than_one(self):
        target = price_target(0, settings)
        assert compute_price(3, target * 1000, 0, settings) == 4
        assert compute_price(3, 0, 1_000_000, settings) == 2


class TestChannelFactor:
    def test_carrying_its_own_weight_is_neutral(self):
        # 10 ops / 100 subs here, 100 / 1000 globally — the same load.
        assert channel_factor(10, 100, 100, 1000, settings) == 1.0

    def test_congested_channel_is_above_one_quiet_is_below(self):
        # Same global picture (100 ops over 1000 subs), opposite local pictures.
        congested = channel_factor(50, 100, 100, 1000, settings)
        quiet = channel_factor(1, 100, 100, 1000, settings)
        assert congested > 1.0 > quiet

    def test_clamped_to_the_band(self):
        band = settings.FEED_PRICE_CHANNEL_BAND
        assert channel_factor(10**6, 1, 100, 1000, settings) == 1.0 + band
        assert channel_factor(0, 10**6, 100, 1000, settings) == 1.0 - band

    def test_degenerate_inputs_are_neutral(self):
        """No subscribers, no global backlog, no subscriptions anywhere — each would
        divide by zero, and each means "we have no evidence", not "surcharge it"."""
        assert channel_factor(5, 0, 100, 1000, settings) == 1.0
        assert channel_factor(0, 100, 0, 1000, settings) == 1.0
        assert channel_factor(0, 100, 100, 0, settings) == 1.0


class TestChannelPrice:
    def test_scales_the_base_price_within_the_band(self):
        band = settings.FEED_PRICE_CHANNEL_BAND
        base = 20
        congested = channel_price(base, 10**6, 1, 100, 1000, settings)
        quiet = channel_price(base, 0, 10**6, 100, 1000, settings)
        assert congested == round(base * (1 + band))
        assert quiet == round(base * (1 - band))

    def test_neutral_channel_pays_exactly_the_base_price(self):
        assert channel_price(7, 10, 100, 100, 1000, settings) == 7

    def test_stays_within_the_global_bounds(self):
        assert (
            channel_price(settings.FEED_PRICE_MAX, 10**6, 1, 100, 1000, settings)
            == settings.FEED_PRICE_MAX
        )
        assert (
            channel_price(settings.FEED_PRICE_MIN, 0, 10**6, 100, 1000, settings)
            == settings.FEED_PRICE_MIN
        )

    def test_rounds_half_up_not_to_even(self):
        """`round` would resolve 2.5 to 2 and 3.5 to 4; a price should not depend on
        the parity of the number it landed next to."""
        band = settings.FEED_PRICE_CHANNEL_BAND
        assert band == 0.25  # the .5 cases below assume the default band
        assert channel_price(2, 10**6, 1, 100, 1000, settings) == 3  # 2 * 1.25
        assert channel_price(6, 0, 10**6, 100, 1000, settings) == 5  # 6 * 0.75


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

        for i in range(settings.FEED_PRICE_TARGET_MIN_ITEMS * 3):
            await service.enqueue_operation(redis, post_id=i, channel_id=1)

        second = await service.get_price_snapshot(redis)
        assert second == first

    async def test_refresh_price_snapshot_updates_the_shared_value(self, redis: Redis):
        before = await service.get_price_snapshot(redis)

        for i in range(settings.FEED_PRICE_TARGET_MIN_ITEMS * 3):
            await service.enqueue_operation(redis, post_id=i, channel_id=1)

        after = await service.refresh_price_snapshot(redis)
        assert after["price"] > before["price"]
        assert (await service.get_price_snapshot(redis)) == after

    async def test_refresh_only_steps_by_one_even_far_over_target(self, redis: Redis):
        """A queue far above target must not jump the price straight to MAX — only
        one refresh's worth of nudging per call, however large the deviation."""
        before = await service.get_price_snapshot(redis)

        for i in range(settings.FEED_PRICE_TARGET_MIN_ITEMS * 3):
            await service.enqueue_operation(redis, post_id=i, channel_id=1)

        after = await service.refresh_price_snapshot(redis)
        assert after["price"] == before["price"] + 1

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


async def _publish_snapshot(
    redis: Redis, price: int, ops_total: int, subs_total: int, now: float | None = None
) -> dict:
    """Publish a base-price snapshot directly, so per-channel tests can pin the base
    price rather than depend on where the global controller happens to have stepped."""
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


async def _give_subscribers(redis: Redis, channel_id: int, count: int) -> None:
    await redis.sadd(
        keys.channel(channel_id), *[str(uuid.uuid4()) for _ in range(count)]
    )


class TestChannelPrices:
    async def test_congested_channel_costs_more_than_a_quiet_one(self, redis: Redis):
        # Same audience, opposite backlogs, against a global average of 0.1 ops/sub.
        await _publish_snapshot(redis, price=20, ops_total=100, subs_total=1000)
        await _give_subscribers(redis, 61, 100)
        await _give_subscribers(redis, 62, 100)
        await redis.hset(keys.OPS_OUTSTANDING, "61", 50)

        prices = await service.channel_prices(redis, [61, 62])

        band = settings.FEED_PRICE_CHANNEL_BAND
        assert prices[61] == round(20 * (1 + band))
        assert prices[62] == round(20 * (1 - band))

    async def test_price_is_frozen_for_the_window(self, redis: Redis):
        """The quote/charge contract: a price shown on the channel list must still be
        what `create_post` charges, however much the channel's backlog moved since."""
        await _publish_snapshot(redis, price=20, ops_total=100, subs_total=1000)
        await _give_subscribers(redis, 63, 100)

        first = (await service.channel_prices(redis, [63]))[63]
        await redis.hset(keys.OPS_OUTSTANDING, "63", 10_000)
        second = (await service.channel_prices(redis, [63]))[63]

        assert second == first

    async def test_recomputes_once_the_window_has_rolled_over(self, redis: Redis):
        now = time.time()
        await _publish_snapshot(
            redis, price=20, ops_total=100, subs_total=1000, now=now
        )
        # 10 ops over 100 subscribers is exactly the global 0.1, i.e. a neutral factor,
        # so each assertion below reads back the base price it was given.
        await _give_subscribers(redis, 64, 100)
        await redis.hset(keys.OPS_OUTSTANDING, "64", 10)
        assert (await service.channel_prices(redis, [64], now=now))[64] == 20

        later = now + settings.FEED_PRICE_REFRESH_SECONDS + 1
        await _publish_snapshot(
            redis, price=8, ops_total=100, subs_total=1000, now=later
        )
        assert (await service.channel_prices(redis, [64], now=later))[64] == 8

    async def test_channel_with_no_subscribers_pays_the_base_price(self, redis: Redis):
        """A brand-new channel is the one that most needs its first posts; the feed
        already protects its backlog rather than discarding it."""
        await _publish_snapshot(redis, price=12, ops_total=100, subs_total=1000)
        await redis.hset(keys.OPS_OUTSTANDING, "65", 500)

        assert (await service.channel_prices(redis, [65]))[65] == 12

    async def test_snapshot_without_totals_prices_flat(self, redis: Redis):
        """A snapshot published before per-channel pricing existed stays live for up to
        FEED_PRICE_TTL_SECONDS after the deploy — those channels get the plain global
        price for a window rather than an exception."""
        now = time.time()
        await redis.set(
            keys.PRICE_SNAPSHOT,
            json.dumps(
                {
                    "price": 16,
                    "computed_at": now,
                    "expires_at": now + settings.FEED_PRICE_REFRESH_SECONDS,
                }
            ),
            ex=settings.FEED_PRICE_TTL_SECONDS,
        )
        await _give_subscribers(redis, 66, 100)
        await redis.hset(keys.OPS_OUTSTANDING, "66", 10_000)

        assert (await service.channel_prices(redis, [66]))[66] == 16

    async def test_no_channels_asks_redis_nothing(self, redis: Redis):
        assert await service.channel_prices(redis, []) == {}


class TestMaybeRefreshPriceSnapshot:
    async def test_does_not_overwrite_before_expiry(self, redis: Redis):
        """The bug this guards against: several uncoordinated processes each run
        `run_price_refresher` on their own timer (see app/factory.py). If any of them
        overwrote the snapshot before its quoted `expires_at`, every caller who was
        already shown that expiry (e.g. GET /posts/economy) would have been quoted a
        guarantee window that was cut short — a broken promise, not just a stale
        number."""
        first = await service.get_price_snapshot(redis)

        for i in range(settings.FEED_PRICE_TARGET_MIN_ITEMS * 3):
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

        for i in range(settings.FEED_PRICE_TARGET_MIN_ITEMS * 3):
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
