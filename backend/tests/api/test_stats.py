from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.models.channel import Channel
from app.models.user import User
from tests.utils import get_jwt_header, review, subscribe


class TestGetMyStats:
    async def test_fresh_user_stats_are_zeroed_and_locked(
        self, client: AsyncClient, create_user
    ):
        user: User = await create_user()
        resp = await client.get(
            settings.API_PATH + "/stats/me", headers=get_jwt_header(user)
        )
        assert resp.status_code == 200, resp.text
        data = resp.json()
        assert data["reviewed_count"] == 0
        assert data["forwarded_count"] == 0
        assert data["dropped_count"] == 0
        assert data["created_post_count"] == 0
        assert data["unlocked"] is False
        assert data["review_gate"] == settings.RELAY_REVIEW_GATE
        assert len(data["weekly_activity"]) == 7
        assert all(bucket["count"] == 0 for bucket in data["weekly_activity"])

    async def test_stats_reflect_reviews_and_unlock_at_gate(
        self, client: AsyncClient, db: AsyncSession, create_user, create_channel, create_post
    ):
        user: User = await create_user()
        channel: Channel = await create_channel()
        await subscribe(db, user, channel)
        for _ in range(settings.RELAY_REVIEW_GATE):
            post = await create_post(channel=channel)
            await review(db, user, post, "forward")

        resp = await client.get(
            settings.API_PATH + "/stats/me", headers=get_jwt_header(user)
        )
        assert resp.status_code == 200, resp.text
        data = resp.json()
        assert data["reviewed_count"] == settings.RELAY_REVIEW_GATE
        assert data["forwarded_count"] == settings.RELAY_REVIEW_GATE
        assert data["unlocked"] is True
        assert sum(bucket["count"] for bucket in data["weekly_activity"]) == (
            settings.RELAY_REVIEW_GATE
        )
        badge_codes_earned = {
            b["code"] for b in data["badges"] if b["earned"]
        }
        assert "early_adopter" in badge_codes_earned
        assert "streak_5" in badge_codes_earned
