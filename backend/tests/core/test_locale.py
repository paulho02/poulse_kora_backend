"""app.core.locale.parse_accept_language as a pure function."""

from app.core.config import settings
from app.core.locale import parse_accept_language


class TestParseAcceptLanguage:
    def test_missing_header_falls_back_to_default(self, monkeypatch):
        monkeypatch.setattr(settings, "SUPPORTED_LOCALES", ["en", "de"])
        monkeypatch.setattr(settings, "DEFAULT_LOCALE", "en")
        assert parse_accept_language(None) == "en"
        assert parse_accept_language("") == "en"

    def test_exact_supported_match(self, monkeypatch):
        monkeypatch.setattr(settings, "SUPPORTED_LOCALES", ["en", "de"])
        monkeypatch.setattr(settings, "DEFAULT_LOCALE", "en")
        assert parse_accept_language("de") == "de"

    def test_regional_subtag_matches_primary_language(self, monkeypatch):
        monkeypatch.setattr(settings, "SUPPORTED_LOCALES", ["en", "de"])
        monkeypatch.setattr(settings, "DEFAULT_LOCALE", "en")
        assert parse_accept_language("de-DE,de;q=0.9,en;q=0.8") == "de"

    def test_preference_order_is_respected(self, monkeypatch):
        monkeypatch.setattr(settings, "SUPPORTED_LOCALES", ["en", "de"])
        monkeypatch.setattr(settings, "DEFAULT_LOCALE", "en")
        assert parse_accept_language("en-US,de;q=0.5") == "en"

    def test_unsupported_language_falls_back_to_default(self, monkeypatch):
        monkeypatch.setattr(settings, "SUPPORTED_LOCALES", ["en", "de"])
        monkeypatch.setattr(settings, "DEFAULT_LOCALE", "en")
        assert parse_accept_language("fr-FR,fr;q=0.9") == "en"

    def test_skips_unsupported_before_finding_a_supported_one(self, monkeypatch):
        monkeypatch.setattr(settings, "SUPPORTED_LOCALES", ["en", "de"])
        monkeypatch.setattr(settings, "DEFAULT_LOCALE", "en")
        assert parse_accept_language("fr-FR,fr;q=0.9,de;q=0.5") == "de"

    def test_garbage_header_falls_back_to_default(self, monkeypatch):
        monkeypatch.setattr(settings, "SUPPORTED_LOCALES", ["en", "de"])
        monkeypatch.setattr(settings, "DEFAULT_LOCALE", "en")
        assert parse_accept_language("*;q=0.1") == "en"
