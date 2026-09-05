"""Dynamic admission pricing for creating an original post.

The price (in tokens) is nudged, one step at a time, toward whichever side of a
*relative* target the fan-out queue currently sits on — congestion is throttled
relative to how many people are around to work through it, rather than against a
fixed item count that means something different at 5 active users vs 100,000. Kept
pure and config-driven so the curve can be tuned without touching the algorithm.

This module holds the transition only. It is not called per-request: a background
task (`app.feed.service.run_price_refresher`) evaluates it on a timer and publishes
the result as a shared snapshot (`refresh_price_snapshot` / `get_price_snapshot`)
that every reader and every charge reads from instead — otherwise two calls a few
seconds apart could see different `ops_len`/`active_users` values (and, being
stateful, race to apply their own step from a now-stale `prev_price`).
"""

import math

from app.core.config import Settings


def price_target(active_users: int, settings: Settings) -> int:
    """The fan-out queue length considered "healthy" right now.

    `FEED_PRICE_BUFFER_RATIO` of active users, but never below
    `FEED_PRICE_TARGET_MIN_ITEMS` — the floor matters most when active_users is
    small or zero (cold start, a quiet night): a ratio-only target would round to 0
    or 1, and then any queue depth at all reads as "too congested".
    """
    ratio_target = round(settings.FEED_PRICE_BUFFER_RATIO * active_users)
    return max(settings.FEED_PRICE_TARGET_MIN_ITEMS, ratio_target)


def compute_price(
    prev_price: int, ops_len: int, active_users: int, settings: Settings
) -> int:
    """Next admission price to publish one original post.

    Moves `prev_price` by exactly 1 toward more expensive (queue over target) or
    cheaper (queue under target), never more in one tick — a step proportional to
    the distance from target would overshoot when congestion is far off and correct
    back the other way next tick, hunting indefinitely (a pendulum). Within
    `FEED_PRICE_DEADBAND_RATIO` of the target, price is left exactly as it was
    instead of nudged, so it settles rather than oscillating between two adjacent
    values once it's already close.
    """
    target = price_target(active_users, settings)
    deadband = max(1, round(target * settings.FEED_PRICE_DEADBAND_RATIO))
    diff = ops_len - target
    if abs(diff) <= deadband:
        return prev_price
    if diff > 0:
        return min(settings.FEED_PRICE_MAX, prev_price + 1)
    return max(settings.FEED_PRICE_MIN, prev_price - 1)


def channel_factor(
    channel_ops: int,
    channel_subs: int,
    ops_total: int,
    subs_total: int,
    settings: Settings,
) -> float:
    """How much pricier (>1) or cheaper (<1) one channel is than the global price.

    Compares the channel's *share of the backlog* to its *share of the audience*:
    ops-per-subscriber here, over ops-per-subscriber everywhere. 1.0 means the channel
    is carrying exactly its weight; the result is clamped to
    ±FEED_PRICE_CHANNEL_BAND so no channel can drift far from the shared price.

    Deliberately not a function of how many of the channel's subscribers are *active* —
    that would need a whole per-channel presence system. It doesn't need one: work that
    cannot be delivered stays outstanding (`keys.OPS_OUTSTANDING` counts parked ops
    too), so a channel whose audience has gone quiet accumulates backlog and prices
    itself up without anyone counting heads. The cost is that it reacts after the fact
    rather than predicting.

    Every degenerate input collapses to 1.0 (neutral, i.e. just the global price). That
    includes a channel with no subscribers at all: a brand-new empty channel is exactly
    the one that most needs its first few posts, and the worker already protects that
    backlog rather than discarding it (see `has_eligible_recipient`), so surcharging it
    would work against a case the rest of the feed goes out of its way to keep alive.
    """
    if channel_subs <= 0 or ops_total <= 0 or subs_total <= 0:
        return 1.0
    band = settings.FEED_PRICE_CHANNEL_BAND
    load = channel_ops / channel_subs
    load_all = ops_total / subs_total
    return max(1.0 - band, min(1.0 + band, load / load_all))


def channel_price(
    base_price: int,
    channel_ops: int,
    channel_subs: int,
    ops_total: int,
    subs_total: int,
    settings: Settings,
) -> int:
    """The global `base_price` scaled by this channel's factor, clamped to the bounds.

    Multiplicative rather than additive on purpose: channel variance should matter more
    when the whole system is expensive and fade out when it is cheap. The visible
    consequence is that at a base price of 1-2 the band rounds away and every channel
    costs the same — which is the right behaviour, since a base price that low means the
    system wants content and no channel should be adding friction.

    Rounds half *up* (`floor(x + 0.5)`), not with `round`, whose banker's rounding would
    quietly resolve the frequent .5 cases in alternating directions.
    """
    factor = channel_factor(
        channel_ops, channel_subs, ops_total, subs_total, settings
    )
    scaled = math.floor(base_price * factor + 0.5)
    return max(settings.FEED_PRICE_MIN, min(settings.FEED_PRICE_MAX, scaled))
