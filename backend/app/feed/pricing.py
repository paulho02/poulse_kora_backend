"""Dynamic admission pricing for creating an original post.

The price (in tokens) rises with operation-queue congestion, throttling *who* gets
to post when the system is busy. Kept pure and config-driven so the curve can be
tuned without touching the algorithm.

This function is the formula only. It is not called per-request: a background task
(`app.feed.service.run_price_refresher`) evaluates it on a timer and publishes the
result as a shared snapshot (`refresh_price_snapshot` / `get_price_snapshot`) that
every reader and every charge reads from instead — otherwise two calls a few seconds
apart could see different `ops_len` values and quote different prices.
"""

from app.core.config import Settings


def compute_price(ops_len: int, settings: Settings) -> int:
    """Price to publish one original post, given the current `ops` queue length.

    clamp(MIN + ops_len // STEP_ITEMS, MIN, MAX)
    """
    step = settings.FEED_PRICE_STEP_ITEMS
    raw = settings.FEED_PRICE_MIN + (ops_len // step if step > 0 else 0)
    return max(settings.FEED_PRICE_MIN, min(settings.FEED_PRICE_MAX, raw))
