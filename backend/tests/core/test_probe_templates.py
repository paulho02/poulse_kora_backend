"""app.core.probe_templates: the text of a test post.

Every property here fails *silently*. A probe with a missing translation, a lopsided
split between the two verdicts, or two variants that open identically all render
perfectly and simply stop measuring what they were meant to - the score keeps producing
numbers, they just mean less. So these are pinned rather than reviewed by eye, the same
way tests/core/test_email_templates.py pins the markup nobody notices is broken until a
recipient complains.

Deliberately no assertions about the prose itself, which is copy and will change.
"""

from app.core import probe_templates as templates
from app.core.config import settings


class TestCatalogueCompleteness:
    def test_every_variant_exists_in_every_content_language(self):
        """A language with no probe copy is a language whose readers are never
        measured, so their trust sits at neutral forever. `variants_for` degrades to
        skipping it rather than serving English under a German label, which is the
        right failure - but it should not be happening by accident."""
        for language in settings.CONTENT_LANGUAGES:
            missing = [v.code for v in templates.VARIANTS if language not in v.texts]
            assert not missing, f"{language!r} is missing: {missing}"

    def test_every_content_language_is_probeable(self):
        assert sorted(templates.probe_languages()) == sorted(
            settings.CONTENT_LANGUAGES
        )

    def test_variant_codes_are_unique(self):
        codes = [variant.code for variant in templates.VARIANTS]
        assert len(codes) == len(set(codes))


class TestVerdictBalance:
    def test_both_verdicts_are_represented(self):
        kinds = {variant.expected_kind for variant in templates.VARIANTS}
        assert kinds == {templates.FORWARD, templates.DROP}

    def test_the_split_is_close_to_even(self):
        """If most variants asked for the same verdict, "always drop anything that
        looks like a test" would beat reading - which is the one strategy this whole
        mechanism exists to distinguish from reading. It is also what fixes
        TRUST_PROBE_CHANCE_RATE at 0.5: blind answering has to converge there."""
        forwards = sum(
            1 for v in templates.VARIANTS if v.expected_kind == templates.FORWARD
        )
        assert abs(forwards - len(templates.VARIANTS) / 2) <= 1

    def test_expected_kinds_are_real_review_verdicts(self):
        for variant in templates.VARIANTS:
            assert variant.expected_kind in ("forward", "drop")


class TestWordingVaries:
    def test_no_two_variants_open_the_same_way(self):
        """A reader who reacts to the first few words is not reading either."""
        for language in settings.CONTENT_LANGUAGES:
            openers = [
                " ".join(v.texts[language].split()[:4]).lower()
                for v in templates.VARIANTS
            ]
            assert len(openers) == len(set(openers)), language

    def test_no_two_variants_share_a_text(self):
        for language in settings.CONTENT_LANGUAGES:
            texts = [v.texts[language] for v in templates.VARIANTS]
            assert len(texts) == len(set(texts)), language

    def test_translations_actually_differ(self):
        """A copy-pasted English string sitting in the German slot would route a post
        to German readers in a language they may not read - the exact mislabelling the
        language economy exists to correct."""
        for variant in templates.VARIANTS:
            assert variant.texts["en"] != variant.texts["de"], variant.code

    def test_the_instruction_is_never_the_opening_clause(self):
        """The ask is buried on purpose: a probe whose first sentence is the
        instruction can be answered without reading the post, which measures nothing.
        """
        for variant in templates.VARIANTS:
            for language, text in variant.texts.items():
                first_clause = text.split(".")[0].split(",")[0].lower()
                assert "forward" not in first_clause, (variant.code, language)
                assert "drop" not in first_clause, (variant.code, language)
                assert "weiterleit" not in first_clause, (variant.code, language)
                assert "verwirf" not in first_clause, (variant.code, language)


class TestVariantsFor:
    def test_unknown_language_yields_nothing_rather_than_a_fallback(self):
        """Falling back to English here would publish an English post under another
        language's label. Yielding nothing means that language is simply not probed,
        and its readers sit at neutral - honest about the gap rather than hiding it."""
        assert templates.variants_for("xx") == []

    def test_known_language_yields_the_whole_catalogue(self):
        assert len(templates.variants_for("en")) == len(templates.VARIANTS)
