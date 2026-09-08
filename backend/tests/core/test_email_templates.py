"""app.core.email_templates: the rendered verification mail.

These pin the things that are invisible until a real recipient complains — that
the code is present in *both* bodies, that an untranslated locale still produces
a complete mail, and that the markup keeps the properties email clients care
about. They deliberately do not assert on the prose itself, which is copy and
will change.
"""

import pytest

from app.core import email_templates as templates
from app.core.config import settings


class TestVerificationEmail:
    @pytest.mark.parametrize("locale", ["en", "de"])
    def test_every_supported_locale_renders(self, locale):
        subject, text, html = templates.verification_email("482913", locale)

        assert subject
        assert text.strip()
        assert html.strip().startswith("<!doctype html>")

    @pytest.mark.parametrize("locale", ["en", "de"])
    def test_code_appears_in_both_bodies(self, locale):
        """The text alternative is not decoration: a client set to prefer text
        shows it instead of the HTML, so a code missing there is a user who
        cannot verify at all."""
        _, text, html = templates.verification_email("482913", locale)

        assert "482913" in text
        assert "482913" in html

    def test_locales_actually_differ(self):
        en_subject, en_text, _ = templates.verification_email("482913", "en")
        de_subject, de_text, _ = templates.verification_email("482913", "de")

        assert en_subject != de_subject
        assert en_text != de_text

    def test_unknown_locale_falls_back_to_default(self):
        """`parse_accept_language` only ever yields a supported locale, but the
        template must not depend on that - it is called with whatever it is
        given."""
        subject, _, _ = templates.verification_email("482913", "fr")
        default, _, _ = templates.verification_email("482913", settings.DEFAULT_LOCALE)

        assert subject == default

    def test_missing_key_in_a_locale_falls_back_per_key(self, monkeypatch):
        """A half-finished translation must degrade to one English sentence, not
        raise - the mail carries a credential the user is waiting on."""
        monkeypatch.setitem(templates._STRINGS, "de", {"verify_subject": "Nur dies"})

        subject, text, html = templates.verification_email("482913", "de")

        assert subject == "Nur dies"
        assert "482913" in text and "482913" in html

    def test_html_lang_follows_the_locale(self):
        _, _, html = templates.verification_email("482913", "de")

        assert '<html lang="de">' in html

    def test_expiry_is_singular_at_one_minute(self, monkeypatch):
        monkeypatch.setattr(settings, "EMAIL_VERIFICATION_CODE_TTL_SECONDS", 60)

        _, text, _ = templates.verification_email("482913", "en")

        assert "1 minute." in text
        assert "1 minutes" not in text

    def test_expiry_reflects_the_configured_ttl(self, monkeypatch):
        monkeypatch.setattr(settings, "EMAIL_VERIFICATION_CODE_TTL_SECONDS", 25 * 60)

        _, text, _ = templates.verification_email("482913", "en")

        assert "25 minutes" in text


class TestMarkupSurvivesEmailClients:
    """Constraints from app/core/email_templates.py's module docstring. Each of
    these fails silently in exactly one popular client, which is why they are
    asserted rather than left to review."""

    def test_no_external_resources(self):
        """Remote images are blocked by default in most clients and a webfont
        link would be stripped, so either would render as a hole in the mail."""
        _, _, html = templates.verification_email("482913", "en")

        assert "<img" not in html
        assert "http://" not in html
        assert "https://" not in html

    def test_no_stylesheet_or_style_block(self):
        """Gmail strips <style> in some contexts; everything has to be inline."""
        _, _, html = templates.verification_email("482913", "en")

        assert "<style" not in html
        assert "<link" not in html

    def test_no_modern_layout(self):
        """Outlook renders through Word, which supports neither."""
        _, _, html = templates.verification_email("482913", "en")

        assert "display:flex" not in html
        assert "display:grid" not in html

    def test_code_is_selectable_text_not_an_image(self):
        """It has to be copyable, and readable by a screen reader."""
        _, _, html = templates.verification_email("482913", "en")

        assert ">\n                  482913\n" in html.replace("\r\n", "\n")

    def test_interpolated_values_are_escaped(self, monkeypatch):
        """Nothing user-controlled reaches this today - the code is generated
        digits - but the layout is a plain f-string, so the escaping is what
        stops that from being one careless caller away from HTML injection."""
        monkeypatch.setitem(
            templates._STRINGS,
            "en",
            {**templates._STRINGS["en"], "verify_heading": "<script>x</script>"},
        )

        _, _, html = templates.verification_email("482913", "en")

        assert "<script>" not in html
        assert "&lt;script&gt;" in html
