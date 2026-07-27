import asyncio

from redis.asyncio import Redis

from app.core.rate_limit import consume, key


class TestConsume:
    async def test_allows_up_to_the_limit_then_blocks(self, redis: Redis):
        for _ in range(3):
            assert await consume(redis, "test", "u1", 3, 10) == 0
        assert await consume(redis, "test", "u1", 3, 10) > 0

    async def test_budgets_are_per_user(self, redis: Redis):
        assert await consume(redis, "test", "u1", 1, 10) == 0
        assert await consume(redis, "test", "u1", 1, 10) > 0
        assert await consume(redis, "test", "u2", 1, 10) == 0

    async def test_budgets_are_per_scope(self, redis: Redis):
        assert await consume(redis, "scope_a", "u1", 1, 10) == 0
        assert await consume(redis, "scope_b", "u1", 1, 10) == 0

    async def test_retry_after_is_bounded_by_the_window(self, redis: Redis):
        await consume(redis, "test", "u1", 1, 10)
        retry_ms = await consume(redis, "test", "u1", 1, 10)
        assert 0 < retry_ms <= 10_000

    async def test_slot_frees_up_once_it_leaves_the_window(self, redis: Redis):
        """The window slides: a hit older than the window stops counting, rather than
        the whole budget resetting on a fixed boundary."""
        assert await consume(redis, "test", "u1", 1, 0.2) == 0
        assert await consume(redis, "test", "u1", 1, 0.2) > 0
        await asyncio.sleep(0.25)
        assert await consume(redis, "test", "u1", 1, 0.2) == 0

    async def test_key_expires_so_idle_users_cost_nothing(self, redis: Redis):
        await consume(redis, "test", "u1", 5, 10)
        assert 0 < await redis.pttl(key("test", "u1")) <= 10_000

    async def test_log_never_grows_past_the_limit(self, redis: Redis):
        for _ in range(20):
            await consume(redis, "test", "u1", 3, 10)
        assert await redis.zcard(key("test", "u1")) == 3

    async def test_concurrent_calls_cannot_overspend(self, redis: Redis):
        """The check and the record happen in one Lua call, so ten requests racing on
        a budget of three admit exactly three."""
        results = await asyncio.gather(
            *(consume(redis, "test", "u1", 3, 10) for _ in range(10))
        )
        assert results.count(0) == 3
