import json

from redis.asyncio import Redis

from app.core.banner import KEY, clear_banner, get_banner, set_banner


class TestBanner:
    async def test_get_when_unset_returns_none(self, redis: Redis):
        assert await get_banner(redis, "en") is None

    async def test_set_then_get_round_trips(self, redis: Redis):
        set_result = await set_banner(redis, {"en": "maintenance tonight"})
        assert set_result["messages"] == {"en": "maintenance tonight"}
        assert set_result["id"]

        fetched = await get_banner(redis, "en")
        assert fetched == {
            "id": set_result["id"],
            "message": "maintenance tonight",
            "set_at": set_result["set_at"],
        }

    async def test_resolves_to_the_requested_locale(self, redis: Redis):
        await set_banner(
            redis, {"en": "maintenance tonight", "de": "Wartung heute Abend"}
        )

        assert (await get_banner(redis, "de"))["message"] == "Wartung heute Abend"
        assert (await get_banner(redis, "en"))["message"] == "maintenance tonight"

    async def test_falls_back_to_default_locale_when_requested_missing(
        self, redis: Redis
    ):
        await set_banner(redis, {"en": "maintenance tonight"})
        assert (await get_banner(redis, "de"))["message"] == "maintenance tonight"

    async def test_falls_back_to_default_locale_for_unsupported_request(
        self, redis: Redis
    ):
        await set_banner(
            redis, {"en": "maintenance tonight", "de": "Wartung heute Abend"}
        )
        assert (await get_banner(redis, "fr"))["message"] == "maintenance tonight"

    async def test_setting_again_issues_a_new_id(self, redis: Redis):
        """A new `set_banner` call is always a new event, even if the text is
        identical — this is what lets a client's "dismiss forever" of an old
        message not silently swallow a later, genuinely new one."""
        first = await set_banner(redis, {"en": "same text"})
        second = await set_banner(redis, {"en": "same text"})
        assert first["id"] != second["id"]

    async def test_clear_removes_it(self, redis: Redis):
        await set_banner(redis, {"en": "temporary"})
        await clear_banner(redis)
        assert await get_banner(redis, "en") is None

    async def test_reads_pre_locale_shape_as_english_only(self, redis: Redis):
        """Back-compat: a banner written before locales existed stored a bare
        `message: str` instead of `messages: dict[str, str]`."""
        legacy = {"id": "abc123", "message": "old shape", "set_at": 1.0}
        await redis.set(KEY, json.dumps(legacy))

        assert (await get_banner(redis, "en"))["message"] == "old shape"
        assert (await get_banner(redis, "de"))["message"] == "old shape"
