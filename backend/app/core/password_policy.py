"""Password strength rule, enforced only when `settings.REQUIRE_STRONG_PASSWORD` is
on (see `UserManager.validate_password` in `app/deps/users.py`). With it off,
fastapi-users applies no rule at all - any password is accepted, by design.
"""

import re
from typing import TypedDict

from app.core.config import settings

_CHARACTER_CLASSES = (
    re.compile(r"[a-z]"),
    re.compile(r"[A-Z]"),
    re.compile(r"[0-9]"),
    re.compile(r"[^a-zA-Z0-9]"),
)


class PasswordViolation(TypedDict):
    code: str
    params: dict[str, int]


def strength_violations(password: str) -> list[PasswordViolation]:
    """Every rule this password fails, as machine-readable codes the client
    localizes (see `lib/src/core/errors/error_messages.dart`) - replaces a
    previous version that returned an English sentence directly, which the
    client had no way to translate. Empty list means the password passes.

    fastapi-users surfaces this verbatim as `InvalidPasswordException.reason`,
    exposed as `detail.reason` on the `register_invalid_password` /
    `update_user_invalid_password` error.
    """
    violations: list[PasswordViolation] = []

    if len(password) < settings.PASSWORD_MIN_LENGTH:
        violations.append(
            {
                "code": "password_too_short",
                "params": {"min_length": settings.PASSWORD_MIN_LENGTH},
            }
        )

    classes_present = sum(
        1 for pattern in _CHARACTER_CLASSES if pattern.search(password)
    )
    if classes_present < settings.PASSWORD_MIN_CHARACTER_CLASSES:
        violations.append(
            {
                "code": "password_missing_variety",
                "params": {
                    "required_categories": settings.PASSWORD_MIN_CHARACTER_CLASSES,
                    "actual_categories": classes_present,
                },
            }
        )

    return violations
