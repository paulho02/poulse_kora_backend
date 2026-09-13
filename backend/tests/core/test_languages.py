"""The rules for what a language value may be, and how a stale one degrades.

Both directions matter: adding a language to CONTENT_LANGUAGES is additive and safe,
but *removing* one leaves posts and readers still carrying it, and these helpers are
the only thing standing between that and a 500 (or a permanently empty feed).
"""

import pytest

from app.core import languages
from app.core.config import Settings, settings


class TestPostLanguages:
    def test_unspecified_is_offered_last(self):
        """It drives the client's picker, and "no language" belongs under the real
        options rather than among them."""
        assert languages.post_languages()[-1] == languages.UNSPECIFIED
        assert languages.post_languages()[:-1] == settings.CONTENT_LANGUAGES

    def test_readers_are_not_offered_unspecified(self):
        """A post with no language routes through the whole channel, which every
        subscriber is in regardless - so "accepting" it could not be switched off and
        would change nothing when switched on."""
        assert languages.UNSPECIFIED not in languages.reading_languages()

    def test_validation_accepts_configured_languages_and_unspecified(self):
        assert languages.is_post_language("en")
        assert languages.is_post_language(languages.UNSPECIFIED)
        assert not languages.is_post_language("kl")
        assert not languages.is_post_language("")


class TestSanitizeReadingLanguages:
    def test_canonicalizes_order_and_removes_duplicates(self):
        """A canonical form is what makes two equivalent sets compare equal, and so
        what lets the route recognise a no-op update instead of bumping
        settings_revision for every client that re-asserts its preference."""
        assert languages.sanitize_reading_languages(["de", "EN", "de", " en "]) == [
            "en",
            "de",
        ]

    def test_drops_languages_that_are_no_longer_configured(self):
        assert languages.sanitize_reading_languages(["en", "kl"]) == ["en"]

    def test_never_returns_empty(self, monkeypatch):
        """The failure this prevents is silent: a reader whose every chosen language
        has since been removed would have an audience of nowhere and a feed that stays
        empty with no error to explain it."""
        assert languages.sanitize_reading_languages([]) == (
            languages.default_reading_languages()
        )
        assert languages.sanitize_reading_languages(["kl", "xx"]) == (
            languages.default_reading_languages()
        )


class TestDefaultReadingLanguages:
    def test_uses_the_request_locale_when_it_is_a_content_language(self):
        assert languages.default_reading_languages("de") == ["de"]

    def test_falls_back_to_the_default_locale(self):
        assert languages.default_reading_languages("kl") == [settings.DEFAULT_LOCALE]
        assert languages.default_reading_languages(None) == [settings.DEFAULT_LOCALE]

    def test_falls_back_to_the_first_content_language(self, monkeypatch):
        """DEFAULT_LOCALE is about the language the API answers *in* and does not have
        to be a content language at all, so the fallback needs a further step rather
        than an IndexError on the day they diverge."""
        monkeypatch.setattr(settings, "CONTENT_LANGUAGES", ["fr", "de"])
        monkeypatch.setattr(settings, "DEFAULT_LOCALE", "en")

        assert languages.default_reading_languages(None) == ["fr"]

    def test_is_one_language_not_all_of_them(self):
        """Defaulting wide would hand posts nobody can read to exactly the users who
        never visit the setting - the problem the feature exists to fix."""
        assert len(languages.default_reading_languages("en")) == 1


class TestContentLanguagesSetting:
    def test_rejects_an_empty_list(self):
        with pytest.raises(ValueError):
            Settings(CONTENT_LANGUAGES=[])

    def test_rejects_duplicates(self):
        """A duplicate would double-count in `subs:total` - one membership per language
        per subscription - and quietly skew every route's price."""
        with pytest.raises(ValueError):
            Settings(CONTENT_LANGUAGES=["en", "en"])

    def test_rejects_the_reserved_no_language_value(self):
        """As a real entry it would be both "the absence of a language" and a language
        slice, so a post declaring it would take one branch here and the other there."""
        with pytest.raises(ValueError):
            Settings(CONTENT_LANGUAGES=["en", languages.UNSPECIFIED])

    def test_normalizes_case_and_whitespace(self):
        assert Settings(CONTENT_LANGUAGES=[" EN ", "De"]).CONTENT_LANGUAGES == [
            "en",
            "de",
        ]
