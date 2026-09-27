"""The username rule (app/core/username_policy.py) and the names derived for
Google signups (app/core/username.py), which must always obey it."""

import pytest

from app.core.config import settings
from app.core.username import FALLBACK, slugify_username
from app.core.username_policy import is_valid_username, normalize_username


def valid(name: str) -> bool:
    return is_valid_username(
        name,
        min_length=settings.USERNAME_MIN_LENGTH,
        max_length=settings.USERNAME_MAX_LENGTH,
    )


class TestNormalize:
    @pytest.mark.parametrize(
        "raw, expected",
        [
            ("Paul", "paul"),
            ("  paul_2 ", "paul_2"),
            ("ｐａｕｌ", "paul"),  # fullwidth
            ("PEERKOLA", "peerkola"),
        ],
    )
    def test_folds(self, raw: str, expected: str):
        assert normalize_username(raw) == expected

    def test_does_not_strip_what_it_cannot_fold(self):
        """Dropping characters would pick a name for the user; refusing is theirs
        to act on."""
        assert normalize_username("José") == "josé"
        assert not valid(normalize_username("José"))


class TestValid:
    @pytest.mark.parametrize("name", ["abc", "paul_2", "a_b_c", "x" * 30])
    def test_accepts(self, name: str):
        assert valid(name)

    @pytest.mark.parametrize(
        "name",
        [
            "ab",
            "x" * 31,
            "peerkοla",  # Greek omicron
            "ab cd",
            "ab.cd",
            "ab-cd",
            "Paul",  # not normalized
            "李雷雷",
            "",
        ],
    )
    def test_refuses(self, name: str):
        assert not valid(name)


class TestSlugify:
    @pytest.mark.parametrize(
        "seed, expected",
        [
            ("Paul Hoff", "paulhoff"),
            ("José", "jose"),
            # ë decomposes to e + a mark; Ø has no decomposition, so it drops.
            ("Zoë Øster", "zoester"),
            ("李雷", FALLBACK),
            ("Al", FALLBACK),
        ],
    )
    def test_derives(self, seed: str, expected: str):
        assert slugify_username(seed) == expected

    @pytest.mark.parametrize("seed", ["Paul", "José", "李雷", "Al", "x" * 80, "!!"])
    def test_always_valid(self, seed: str):
        assert valid(slugify_username(seed))
