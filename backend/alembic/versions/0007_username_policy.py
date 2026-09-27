# ruff: noqa: E501,I001
"""normalize usernames to [a-z0-9_] and enforce it with a CHECK constraint

Usernames were unique by exact match and otherwise unrestricted, so `Peerkola`,
`peerkοla` (Greek omicron) and ` peerkola` could all coexist - impersonation of
the probe author and of each other. From here on a username is lowercase ASCII
letters, digits and `_` (app/core/username_policy.py); `UserManager` refuses
anything else as `username_invalid`.

This rewrites the rows that predate the rule, then adds the CHECK constraint so
no writer can reintroduce one. Rewriting rather than grandfathering, so the
invariant is total and "exact match" really does mean "same name".

How a name is rewritten:

- names that already obey the rule are untouched, and they win every clash -
  whoever already holds `paul` keeps it;
- the rest are NFKC-folded and lowercased (so `Paul` becomes `paul` when that
  is free), accents are folded to their base letter, anything else outside
  `[a-z0-9_]` is dropped, and the result is truncated to the maximum length;
- too short afterwards (a name entirely in another script, say) falls back to
  `user`;
- a clash with a name already held - or with the reserved probe author name -
  gets a numeric suffix (`paul2`, `paul3`, ...). Processed oldest account first,
  so between two old `Paul`/`PAUL` rows the older one gets the plain name.

Affected users are not notified; the new name simply shows on their profile.
The bounds are pinned here rather than read from settings: a data migration has
to keep producing the same result after the settings move on. The probe author's
name is read from settings, as 0005 does, because it is the name that must stay
reserved *now*.

Downgrade drops the constraint only. The original names are not recoverable and
are not needed - the old rule accepted every name the new one produces.

Revision ID: 0007_username_policy
Revises: 0006_rename_probe_author
Create Date: 2026-09-26 12:00:00.000000

"""

import re
import unicodedata

from alembic import op
import sqlalchemy as sa

from app.core.config import settings

# revision identifiers, used by Alembic.
revision = "0007_username_policy"
down_revision = "0006_rename_probe_author"
branch_labels = None
depends_on = None

_MIN_LENGTH = 3
_MAX_LENGTH = 30
_FALLBACK = "user"
_VALID = re.compile(r"[a-z0-9_]+")

_CONSTRAINT = "ck_users_username_charset"


def _is_valid(name: str) -> bool:
    return _MIN_LENGTH <= len(name) <= _MAX_LENGTH and _VALID.fullmatch(name) is not None


def _base(name: str) -> str:
    folded = unicodedata.normalize("NFKC", name).strip().lower()
    ascii_only = (
        unicodedata.normalize("NFKD", folded).encode("ascii", "ignore").decode("ascii")
    )
    slug = re.sub(r"[^a-z0-9_]", "", ascii_only)[:_MAX_LENGTH]
    return slug if len(slug) >= _MIN_LENGTH else _FALLBACK


def _unique(base: str, taken: set[str]) -> str:
    if base not in taken:
        return base
    n = 2
    while True:
        suffix = str(n)
        candidate = base[: _MAX_LENGTH - len(suffix)] + suffix
        if candidate not in taken:
            return candidate
        n += 1


def upgrade():
    conn = op.get_bind()
    rows = conn.execute(
        sa.text(
            "SELECT id, username FROM users WHERE username IS NOT NULL "
            "ORDER BY created, id"
        )
    ).all()

    taken = {name for _, name in rows if _is_valid(name)}
    taken.add(settings.TRUST_PROBE_AUTHOR_USERNAME)

    for user_id, name in rows:
        if _is_valid(name):
            continue
        new_name = _unique(_base(name), taken)
        taken.add(new_name)
        conn.execute(
            sa.text("UPDATE users SET username = :username WHERE id = :id"),
            {"username": new_name, "id": user_id},
        )

    op.create_check_constraint(_CONSTRAINT, "users", "username ~ '^[a-z0-9_]+$'")


def downgrade():
    op.drop_constraint(_CONSTRAINT, "users", type_="check")
