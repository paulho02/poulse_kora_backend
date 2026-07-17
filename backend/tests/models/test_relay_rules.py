from app.core.config import settings
from app.core.relay_rules import (
    compute_avg_hops,
    compute_badges,
    compute_trust_score,
    is_review_gate_unlocked,
)
from app.models.user import User


def _user(**overrides) -> User:
    defaults = dict(
        reviewed_count=0,
        forwarded_count=0,
        dropped_count=0,
        is_superuser=False,
    )
    defaults.update(overrides)
    return User(**defaults)


def test_review_gate_locked_below_threshold():
    user = _user(reviewed_count=settings.RELAY_REVIEW_GATE - 1)
    assert is_review_gate_unlocked(user) is False


def test_review_gate_unlocked_at_threshold():
    user = _user(reviewed_count=settings.RELAY_REVIEW_GATE)
    assert is_review_gate_unlocked(user) is True


def test_superuser_always_unlocked():
    user = _user(reviewed_count=0, is_superuser=True)
    assert is_review_gate_unlocked(user) is True


def test_avg_hops_zero_when_no_reviews():
    assert compute_avg_hops(_user()) == 0.0


def test_avg_hops_ratio_of_forwarded_to_reviewed():
    user = _user(reviewed_count=10, forwarded_count=4)
    assert compute_avg_hops(user) == 0.4


def test_trust_score_is_clamped_between_0_and_100():
    low = _user(dropped_count=1000)
    high = _user(reviewed_count=1000, forwarded_count=1000)
    assert compute_trust_score(low) == 0
    assert compute_trust_score(high) == 100


def test_compute_badges_shape_and_thresholds():
    user = _user(reviewed_count=5)
    badges = compute_badges(user, trust_score=80)
    codes = {b.code: b.earned for b in badges}
    assert codes["early_adopter"] is True
    assert codes["trusted_curator"] is True
    assert codes["streak_5"] is True
    assert codes["streak_10"] is False
