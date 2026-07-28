from redis.asyncio import Redis

from app.core.banner import clear_banner, get_banner, set_banner


class TestBanner:
    async def test_get_when_unset_returns_none(self, redis: Redis):
        assert await get_banner(redis) is None

    async def test_set_then_get_round_trips(self, redis: Redis):
        set_result = await set_banner(redis, "maintenance tonight")
        assert set_result["message"] == "maintenance tonight"
        assert set_result["id"]

        fetched = await get_banner(redis)
        assert fetched == set_result

    async def test_setting_again_issues_a_new_id(self, redis: Redis):
        """A new `set_banner` call is always a new event, even if the text is
        identical — this is what lets a client's "dismiss forever" of an old
        message not silently swallow a later, genuinely new one."""
        first = await set_banner(redis, "same text")
        second = await set_banner(redis, "same text")
        assert first["id"] != second["id"]

    async def test_clear_removes_it(self, redis: Redis):
        await set_banner(redis, "temporary")
        await clear_banner(redis)
        assert await get_banner(redis) is None
