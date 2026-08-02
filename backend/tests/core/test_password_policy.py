"""app.core.password_policy.strength_violations as a pure function.

The API-level tests (tests/api/test_users.py) only exercise the length rule via
one short password; the character-class-diversity rule (a password long enough
but drawn from only one character class) had no coverage at all."""

from app.core.config import settings
from app.core.password_policy import strength_violations


class TestStrengthViolations:
    def test_too_short_is_a_violation(self, monkeypatch):
        monkeypatch.setattr(settings, "PASSWORD_MIN_LENGTH", 10)
        violations = strength_violations("Ab1!")
        assert {"code": "password_too_short", "params": {"min_length": 10}} in (
            violations
        )

    def test_long_but_single_character_class_is_a_violation(self, monkeypatch):
        monkeypatch.setattr(settings, "PASSWORD_MIN_LENGTH", 10)
        monkeypatch.setattr(settings, "PASSWORD_MIN_CHARACTER_CLASSES", 3)
        violations = strength_violations("aaaaaaaaaaaaaa")  # long, lowercase-only
        assert {
            "code": "password_missing_variety",
            "params": {"required_categories": 3, "actual_categories": 1},
        } in violations

    def test_passes_length_and_class_requirements(self, monkeypatch):
        monkeypatch.setattr(settings, "PASSWORD_MIN_LENGTH", 10)
        monkeypatch.setattr(settings, "PASSWORD_MIN_CHARACTER_CLASSES", 3)
        assert strength_violations("Sup3rSecret!") == []

    def test_exactly_at_the_class_threshold_passes(self, monkeypatch):
        monkeypatch.setattr(settings, "PASSWORD_MIN_LENGTH", 4)
        monkeypatch.setattr(settings, "PASSWORD_MIN_CHARACTER_CLASSES", 2)
        assert strength_violations("abAB") == []  # exactly 2 classes: lower + upper

    def test_short_and_low_variety_reports_both_violations(self, monkeypatch):
        monkeypatch.setattr(settings, "PASSWORD_MIN_LENGTH", 10)
        monkeypatch.setattr(settings, "PASSWORD_MIN_CHARACTER_CLASSES", 3)
        violations = strength_violations("weak")
        codes = {v["code"] for v in violations}
        assert codes == {"password_too_short", "password_missing_variety"}
