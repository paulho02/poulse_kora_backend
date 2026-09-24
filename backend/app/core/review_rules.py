"""Pure MVP heuristics for the review gate and the badges on the stats screen.

There is no real hop-propagation graph in this MVP (every subscriber of a channel
sees every post in it — see CLAUDE.md/plan), so `avg_hops` is a simple, documented proxy
rather than something derived from real propagation data. Keep these as pure functions
(no I/O) so they're shared identically between the review-gate check (app/api/posts.py)
and the stats display (app/api/stats.py), and are trivially unit-testable.

The trust score used to live here too, as a lifetime-weighted sum of the three counters
below. It is now a real measurement with real consequences - it decides how far a
forward travels - and lives in app/core/trust.py, which needs a window of history rather
than a row. `compute_badges` takes the score as an argument precisely so this module
stays I/O-free.
"""

from app.core.config import settings
from app.models.user import User
from app.schemas.stats import BadgeRead


def is_review_gate_unlocked(user: User) -> bool:
    return user.is_superuser or user.reviewed_count >= settings.REVIEW_GATE


def compute_avg_hops(user: User) -> float:
    """Proxy for "how often a review becomes a forward" — not a real hop count."""
    if user.reviewed_count == 0:
        return 0.0
    return round(user.forwarded_count / user.reviewed_count, 2)


def compute_badges(user: User, trust_score: int) -> list[BadgeRead]:
    return [
        BadgeRead(code="early_adopter", label="Early Adopter", earned=True),
        BadgeRead(
            code="trusted_curator",
            label="Trusted Curator",
            earned=trust_score >= 75,
        ),
        BadgeRead(
            code="streak_5", label="5× Streak", earned=user.reviewed_count >= 5
        ),
        BadgeRead(
            code="streak_10", label="10× Streak", earned=user.reviewed_count >= 10
        ),
    ]
