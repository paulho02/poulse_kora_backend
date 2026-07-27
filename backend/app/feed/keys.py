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


def queue(user_id: str) -> str:
    """Per-user review queue (list of post_ids)."""
    return f"queue:{user_id}"


def channel(channel_id: int) -> str:
    """Set of subscriber user_ids for a channel."""
    return f"channel:{channel_id}"


def tokens(user_id: str) -> str:
    """Spendable token balance (atomic counter)."""
    return f"tokens:{user_id}"


def seen(post_id: int) -> str:
    """Set of user_ids a post has already been delivered to (the re-delivery guard).

    Written by the `place` script in the same atomic call that pushes the post into a
    queue, so membership is recorded before the recipient can act on it. Expires after
    FEED_SEEN_TTL_SECONDS, refreshed on each delivery — the set dies with the post
    rather than accumulating forever.
    """
    return f"seen:{post_id}"
