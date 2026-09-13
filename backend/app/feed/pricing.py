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


def route_factor(
    route_ops: int,
    route_subs: int,
    ops_total: int,
    subs_total: int,
    settings: Settings,
) -> float:
    """How much pricier (>1) or cheaper (<1) one route is than the global price.

    A route is a (channel, language) pair — the unit that has its own audience, and so
    the unit that can be congested on its own. Compares the route's *share of the
    backlog* to its *share of the audience*: ops-per-subscriber here, over
    ops-per-subscriber everywhere. 1.0 means the route is carrying exactly its weight;
    the result is clamped to ±FEED_PRICE_CHANNEL_BAND (±50%) so no route can drift far
    from the shared price.

    This is congestion pricing, and deliberately *not* compensation for a small
    audience. It is worth being explicit, because the two are easy to confuse now that
    routes differ in size: a post's tokens buy FEED_FANOUT deliveries whatever route it
    takes, and every forward after that is earned rather than bought, so a
    minority-language author already pays the same price for the same guaranteed reach.
    What they get less of is upside, which no admission price can hand back. What this
    factor protects is a small route's *queue* — a handful of readers should not absorb
    the same posting rate as a large one.

    Deliberately not a function of how many of the route's subscribers are *active* —
    that would need a whole per-route presence system. It doesn't need one: work that
    cannot be delivered stays outstanding (`keys.OPS_OUTSTANDING` counts parked ops
    too), so a route whose audience has gone quiet accumulates backlog and prices
    itself up without anyone counting heads. The cost is that it reacts after the fact
    rather than predicting.

    Every degenerate input collapses to 1.0 (neutral, i.e. just the global price). That
    includes a route with no subscribers at all: a brand-new empty channel — or a
    language nobody has opted into yet — is exactly the case that most needs its first
    few posts, and the worker already protects that backlog rather than discarding it
    (see `has_eligible_recipient`), so surcharging it would work against a case the
    rest of the feed goes out of its way to keep alive.
    """
    if route_subs <= 0 or ops_total <= 0 or subs_total <= 0:
        return 1.0
    band = settings.FEED_PRICE_CHANNEL_BAND
    load = route_ops / route_subs
    load_all = ops_total / subs_total
    return max(1.0 - band, min(1.0 + band, load / load_all))


def route_price(
    base_price: int,
    route_ops: int,
    route_subs: int,
    ops_total: int,
    subs_total: int,
    settings: Settings,
) -> int:
    """The global `base_price` scaled by this route's factor, clamped to the bounds.

    Multiplicative rather than additive on purpose: route variance should matter more
    when the whole system is expensive and fade out when it is cheap. The visible
    consequence is that at a base price of 1-2 the band rounds away and every route
    costs the same — which is the right behaviour, since a base price that low means the
    system wants content and no route should be adding friction.

    The band is what decides whether the range the client shows says anything. At ±25%
    the whole spread across the deployment rounded down to one or two tokens; at ±50% a
    base price of 4 spans 2-6. It still collapses at the very bottom of the scale (a
    base price of 1 can only be 1 or 2), which remains correct: a price that low means
    the system wants content.

    Rounds half *up* (`floor(x + 0.5)`), not with `round`, whose banker's rounding would
    quietly resolve the frequent .5 cases in alternating directions.
    """
    factor = route_factor(route_ops, route_subs, ops_total, subs_total, settings)
    scaled = math.floor(base_price * factor + 0.5)
    return max(settings.FEED_PRICE_MIN, min(settings.FEED_PRICE_MAX, scaled))
