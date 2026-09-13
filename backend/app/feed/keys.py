"""Redis key builders for the feed distribution algorithm.

All keys live in a single logical DB (recipient selection reads a channel set and
`free_queue` together, and the whole feed shares one connection). DB 1 is reserved
for tests (see config).
"""

from app.core.config import LANGUAGE_UNSPECIFIED as UNSPECIFIED

# The operation stream: fan-out jobs (fields post_id/channel_id) awaiting distribution.
# A Redis Stream consumed by a consumer group (STREAM_GROUP), which is what makes the
# queue both crash-safe (unacked entries are reclaimable) and horizontally scalable
# (several consumers can share the group). Entries are XACK'd + XDEL'd once fanned out,
# so the stream self-trims to outstanding work (see app/feed/worker.py).
STREAM = "feed:ops"

# The consumer group over STREAM. Every worker process joins this one group under a
# distinct consumer name, so each entry is delivered to exactly one worker.
STREAM_GROUP = "feed:workers"

# Operations undeliverable on their last attempt (no free recipient), parked here as
# a sorted set (score = ready-at unix timestamp) until due to be re-added to the stream.
OPS_RETRY = "ops:retry"

# Users with at least one free slot in their review queue.
FREE_QUEUE = "free_queue"

# Shared admission-price snapshot ({"price", "computed_at", "expires_at"} JSON),
# refreshed by a background task every FEED_PRICE_REFRESH_SECONDS (see
# app/feed/service.py: refresh_price_snapshot). One key for the whole deployment, so
# every reader and every charge agree on the same price for the same window instead of
# each computing its own live value.
PRICE_SNAPSHOT = "feed:price"

# Sorted set of user_ids by last-activity unix timestamp (see service.mark_active /
# active_user_count). One key for the whole deployment; membership is a sliding
# window (score >= now - ACTIVE_USER_WINDOW_SECONDS counts as active), not a set with
# TTL'd members, since Redis has no per-member expiry.
ACTIVE_USERS = "active_users"

# Hash of route (see `route()`) -> outstanding fan-out ops for that route, the
# numerator of the per-route price factor (see app/feed/pricing.py: route_factor).
# Incremented where new work is minted (`enqueue_operation`) and decremented at a
# terminal outcome (delivered, or abandoned) — retry churn deliberately does not touch
# it, so a post parked in `ops:retry` still counts as outstanding. That is the whole
# point: work that cannot find a recipient is exactly what should make a route look
# congested.
#
# Keyed by route rather than by channel since language routing landed, because a route
# is what has its own audience: a channel's German slice can be saturated while its
# English slice still has room, and one number across both would price each of them by
# the other's congestion.
#
# A hash rather than a key per route so a page of routes costs one HMGET, and the
# global total is one HVALS. Approximate by nature (see `retire_operation`) — it
# prices, it does not gate delivery.
OPS_OUTSTANDING = "feed:ops:outstanding"

# Total *audience memberships* across the deployment (the denominator's denominator —
# see `route_factor`). A running counter maintained by sync_subscribe/sync_unsubscribe
# because the alternative, summing SCARD over every audience set, is the one part of
# the factor that would otherwise need to enumerate channels.
#
# Memberships, not subscriptions: one subscription puts a reader into the plain channel
# set *and* into one set per language they accept, and `route_factor` divides a single
# route's subscriber count by this. Counting subscriptions instead would compare a
# slice against a whole, making every language route look under-subscribed by roughly
# the average number of languages a reader accepts.
SUBS_TOTAL = "subs:total"


def route(channel_id: int, language: str) -> str:
    """The routing key in its string form: `"{channel_id}:{language}"`.

    This - not the channel id - is the unit that op accounting (`OPS_OUTSTANDING`)
    and pricing are keyed by, because it is the unit that has its own audience and
    therefore its own congestion. `OPS_OUTSTANDING` was already a string-keyed hash,
    so widening the key cost nothing there.

    One upgrade note: entries written before routing existed are keyed by a bare
    channel id and are simply orphaned by this - no route ever matches them. They are
    a pricing input, not a delivery decision, and `reseed_outstanding_ops` recounts
    the hash from the ops actually in flight, so a rebuild clears them.
    """
    return f"{channel_id}:{language}"


def audience(channel_id: int, language: str) -> str:
    """The set of user_ids a post in (`channel_id`, `language`) may be delivered to.

    The whole language feature is this function. Fan-out samples recipients with one
    `SRANDMEMBER` against a set that already exists (see `service.select_recipients`),
    and that stays true here: a language is not a filter applied after sampling, it is
    part of the key of the set being sampled. So the hot path pays *nothing* for
    language routing - same one call, different key.

    That is why filtering after the sample was rejected instead. With a sample of
    `FEED_FANOUT * FEED_FANOUT_SAMPLE_MULTIPLIER` candidates, a language spoken by a
    tenth of a channel yields roughly one usable recipient per operation, so nearly
    every op would park, retry, and inflate the admission price for everyone. Sampling
    from the right population is the difference between O(sample) and O(audience).

    UNSPECIFIED returns the plain channel set, deliberately: a post with no language
    is readable by everyone, and the channel set is already exactly the union of its
    language slices. So universal posts need no set of their own, and they remain
    deliverable to a reader whose own language has almost no supply.
    """
    if language == UNSPECIFIED:
        return channel(channel_id)
    return f"channel:{channel_id}:lang:{language}"


def route_price(channel_id: int, language: str) -> str:
    """Cached admission price for one route ({"price", "expires_at"} JSON).

    Populated lazily by `service.route_prices` and stamped with the *global*
    snapshot's `expires_at`, so a route's price is frozen for exactly the window its
    base price is, and the two roll over together. Without this the price quoted on a
    channel list and the price charged by `create_post` could differ, which is the
    same broken promise `PRICE_SNAPSHOT` exists to prevent.
    """
    return f"feed:price:route:{channel_id}:{language}"


# Observed spread of route prices in the current price window, as a hash of
# {window, min, max} (see `service.observe_price`). The client shows a range rather
# than one number, because there is no longer one number: every (channel, language)
# route prices itself.
#
# Deliberately *observed* rather than computed. Answering "what is the cheapest route
# right now" exactly would mean pricing every route on every window tick, which needs
# the price refresher to enumerate channels out of Postgres - work proportional to
# channels x languages, repeated whether or not anyone is looking. Instead every route
# price that gets computed for a real request widens this, and `GET /channels` prices
# every route of the channels it was already listing. The range therefore converges
# within the first channel-list load of each window and costs no background work at
# all. The visible cost is that a window nobody has priced a route in yet has observed
# nothing of its own — so the hash also records the base price each spread was measured
# against, and `service.read_price_range` rescales the previous window's spread onto the
# current base instead of collapsing to a flat range (which read as "every route costs
# the same" once a minute, and undercut what create_post would charge).
PRICE_RANGE = "feed:price:range"


def channel_price_range(channel_id: int) -> str:
    """Observed spread of one channel's route prices (see PRICE_RANGE)."""
    return f"feed:price:range:channel:{channel_id}"


def queue(user_id: str) -> str:
    """Per-user review queue (list of post_ids)."""
    return f"queue:{user_id}"


def channel(channel_id: int) -> str:
    """Set of *all* subscriber user_ids for a channel, whatever language they read.

    Kept alongside the per-language sets rather than replaced by them, for three jobs
    it is the only answer to: it is the audience of an UNSPECIFIED post (see
    `audience`), it is the subscriber count a channel's own price is scaled by, and it
    is what `purge_user`/`sync_unsubscribe` clear membership from without having to
    know which languages the reader accepted at the time they subscribed.
    """
    return f"channel:{channel_id}"


def tokens(user_id: str) -> str:
    """Spendable token balance (atomic counter)."""
    return f"tokens:{user_id}"


def deleted_post(post_id: int) -> str:
    """Tombstone marking a post whose row has been erased (see
    app/core/account_deletion.py).

    The worker is deliberately pure-Redis - it fans out from the stream entry
    alone and never opens a Postgres session - so "has this post been deleted?"
    has to be answerable from here or not at all. Without it, every operation
    already in the stream or parked in `ops:retry` when an author erased their
    posts would keep placing ids that resolve to nothing, manufacturing fresh
    ghost cards in strangers' feeds for up to FEED_RETRY_MAX_AGE_SECONDS.

    TTL is exactly FEED_RETRY_MAX_AGE_SECONDS: no op can be minted for a post
    that no longer exists (creating and forwarding both read the row first), so
    the last op that could still chase this id is one parked immediately before
    the deletion, whose own deadline is that far out. Expiring with it leaves
    nothing behind.
    """
    return f"deleted:{post_id}"


def seen(post_id: int) -> str:
    """Set of user_ids a post has already been delivered to (the re-delivery guard).

    Written by the `place` script in the same atomic call that pushes the post into a
    queue, so membership is recorded before the recipient can act on it. Expires after
    FEED_SEEN_TTL_SECONDS (derived from FEED_RETRY_MAX_AGE_SECONDS, so it always
    outlives the ops still delivering this post), refreshed on each delivery — the set
    dies with the post rather than accumulating forever.
    """
    return f"seen:{post_id}"
