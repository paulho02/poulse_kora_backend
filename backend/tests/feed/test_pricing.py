from app.core.config import settings
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
