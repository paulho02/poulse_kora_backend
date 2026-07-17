"""Pure MVP heuristics for the Relay feature.

There is no real relay/hop-propagation graph in this MVP (every subscriber of a channel
sees every post in it — see CLAUDE.md/plan), so `trust_score` and `avg_hops` are simple,
documented proxies rather than derived from real propagation data. Keep these as pure
functions (no I/O) so they're shared identically between the review-gate check
(app/api/posts.py) and the stats display (app/api/stats.py), and are trivially unit-testable.
"""

from app.core.config import settings
from app.models.user import User
from app.schemas.stats import BadgeRead


def is_review_gate_unlocked(user: User) -> bool:
    return user.is_superuser or user.reviewed_count >= settings.RELAY_REVIEW_GATE


def compute_avg_hops(user: User) -> float:
    """Proxy for "how often a review becomes a forward" — not a real hop count."""
    if user.reviewed_count == 0:
        return 0.0
    return round(user.forwarded_count / user.reviewed_count, 2)


def compute_trust_score(user: User) -> int:
    """Simple weighted heuristic, clamped to 0-100. Not a source of truth to persist."""
    score = (
        50
        + user.reviewed_count * 1.5
        + user.forwarded_count * 1
        - user.dropped_count * 0.5
    )
    return max(0, min(100, round(score)))


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
