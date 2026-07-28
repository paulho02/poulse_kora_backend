"""Password strength rule, enforced only when `settings.REQUIRE_STRONG_PASSWORD` is
on (see `UserManager.validate_password` in `app/deps/users.py`). With it off,
fastapi-users applies no rule at all - any password is accepted, by design.
"""

import re

from app.core.config import settings

_CHARACTER_CLASSES = (
    re.compile(r"[a-z]"),
    re.compile(r"[A-Z]"),
    re.compile(r"[0-9]"),
    re.compile(r"[^a-zA-Z0-9]"),
)


def strength_violation(password: str) -> str | None:
    """A human-readable reason the password is too weak, or None if it passes.

    fastapi-users surfaces this verbatim as `InvalidPasswordException.reason`, which
    the API exposes as `detail.reason` on the `register_invalid_password` /
    `update_user_invalid_password` error - the client can render it directly.
    """
    if len(password) < settings.PASSWORD_MIN_LENGTH:
        return (
            f"Password must be at least {settings.PASSWORD_MIN_LENGTH} characters "
            "long."
        )

    classes_present = sum(
        1 for pattern in _CHARACTER_CLASSES if pattern.search(password)
    )
    if classes_present < settings.PASSWORD_MIN_CHARACTER_CLASSES:
        return (
            "Password must include at least "
            f"{settings.PASSWORD_MIN_CHARACTER_CLASSES} of: lowercase letters, "
            "uppercase letters, numbers, and symbols."
        )

    return None
