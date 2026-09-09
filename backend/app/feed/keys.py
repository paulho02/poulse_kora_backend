"""Redis key builders for the feed distribution algorithm.

All keys live in a single logical DB (recipient selection reads a channel set and
`free_queue` together, and the whole feed shares one connection). DB 1 is reserved
for tests (see config).
"""

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

# Hash of channel_id -> outstanding fan-out ops for that channel, the numerator of the
# per-channel price factor (see app/feed/pricing.py: channel_factor). Incremented where
# new work is minted (`enqueue_operation`) and decremented at a terminal outcome
# (delivered, or abandoned) — retry churn deliberately does not touch it, so a post
# parked in `ops:retry` still counts as outstanding. That is the whole point: work that
# cannot find a recipient is exactly what should make a channel look congested.
#
# A hash rather than a key per channel so a page of channels costs one HMGET, and the
# global total is one HVALS on the refresher tick. Approximate by nature (see
# `retire_operation`) — it prices, it does not gate delivery.
OPS_OUTSTANDING = "feed:ops:outstanding"

# Total channel subscriptions across the deployment (the denominator's denominator —
# see `channel_factor`). A running counter maintained by sync_subscribe/sync_unsubscribe
# because the alternative, summing SCARD over every channel, is the one part of the
# factor that would otherwise need to enumerate channels.
SUBS_TOTAL = "subs:total"


def channel_price(channel_id: int) -> str:
    """Cached admission price for one channel ({"price", "expires_at"} JSON).

    Populated lazily by `service.channel_prices` and stamped with the *global*
    snapshot's `expires_at`, so a channel's price is frozen for exactly the window its
    base price is, and the two roll over together. Without this the price quoted on a
    channel list and the price charged by `create_post` could differ, which is the
    same broken promise `PRICE_SNAPSHOT` exists to prevent.
    """
    return f"feed:price:channel:{channel_id}"


def queue(user_id: str) -> str:
    """Per-user review queue (list of post_ids)."""
    return f"queue:{user_id}"


def channel(channel_id: int) -> str:
    """Set of subscriber user_ids for a channel."""
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
