"""Redis key builders for the feed distribution algorithm.

All keys live in a single logical DB: `SINTER`/`SINTERSTORE` (used to intersect a
channel's subscribers with the free-slot set) only works within one DB, so channel
sets and `free_queue` must share it. DB 1 is reserved for tests (see config).
"""

# The operation queue: JSON items {"post_id", "channel_id"} awaiting fan-out.
OPS = "ops"

# Operations undeliverable on their last attempt (no free recipient), parked here as
# a sorted set (score = ready-at unix timestamp) until due for another try.
OPS_RETRY = "ops:retry"

# Users with at least one free slot in their review queue.
FREE_QUEUE = "free_queue"


def queue(user_id: str) -> str:
    """Per-user review queue (list of post_ids)."""
    return f"queue:{user_id}"


def channel(channel_id: int) -> str:
    """Set of subscriber user_ids for a channel."""
    return f"channel:{channel_id}"


def tokens(user_id: str) -> str:
    """Spendable token balance (atomic counter)."""
    return f"tokens:{user_id}"
