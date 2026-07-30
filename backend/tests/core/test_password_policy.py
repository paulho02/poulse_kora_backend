"""app.core.password_policy.strength_violation as a pure function.

The API-level tests (tests/api/test_users.py) only exercise the length rule via
one short password; the character-class-diversity rule (a password long enough
but drawn from only one character class) had no coverage at all."""

from app.core.config import settings
from app.core.password_policy import strength_violation


class TestStrengthViolation:
    def test_too_short_is_a_violation(self, monkeypatch):
        monkeypatch.setattr(settings, "PASSWORD_MIN_LENGTH", 10)
        reason = strength_violation("Ab1!")
        assert reason is not None
        assert "at least 10 characters" in reason

    def test_long_but_single_character_class_is_a_violation(self, monkeypatch):
        monkeypatch.setattr(settings, "PASSWORD_MIN_LENGTH", 10)
        monkeypatch.setattr(settings, "PASSWORD_MIN_CHARACTER_CLASSES", 3)
        reason = strength_violation("aaaaaaaaaaaaaa")  # long, lowercase-only
        assert reason is not None
        assert "at least 3 of" in reason

    def test_passes_length_and_class_requirements(self, monkeypatch):
        monkeypatch.setattr(settings, "PASSWORD_MIN_LENGTH", 10)
        monkeypatch.setattr(settings, "PASSWORD_MIN_CHARACTER_CLASSES", 3)
        assert strength_violation("Sup3rSecret!") is None

    def test_exactly_at_the_class_threshold_passes(self, monkeypatch):
        monkeypatch.setattr(settings, "PASSWORD_MIN_LENGTH", 4)
        monkeypatch.setattr(settings, "PASSWORD_MIN_CHARACTER_CLASSES", 2)
        assert strength_violation("abAB") is None  # exactly 2 classes: lower + upper
