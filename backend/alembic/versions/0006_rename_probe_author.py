# ruff: noqa: E501,I001
"""rename probe author to the Peerkola identity

The app was renamed to Peerkola, which moved `TRUST_PROBE_AUTHOR_EMAIL` and
`TRUST_PROBE_AUTHOR_USERNAME` (app/core/config.py). A fresh database is fine
without this: 0005 mints the row from those settings, so it is minted under the
new identity already. An *existing* database is not - its row still carries the
old pair, and `_ensure_probe_author` (app/core/probes.py) looks the author up by
the configured email. Without this migration it would find nothing, mint a
second author, and leave every probe already answered attached to an orphan row
whose username stays reserved for a name nobody uses.

So the rename happens here, where a live database's only route is. The old pair
is hardcoded rather than read from settings, because settings now hold the new
one - a data migration has to pin the values it migrates *from*.

Three guards, all of which make this a no-op rather than a failure:

- `is_active = false` - the probe author is an identity, not a login (see 0005).
  A row at the old address that someone *can* log into is not ours to rename.
- no row at the new email already - covers a fresh database (0005 minted it
  under the new identity, so there is nothing to rename) and a re-run.
- no row already holding the new username, case-insensitively - `UserManager`
  reserves the configured name, but only from the moment the setting changed, so
  an account registered as "peerkola" before then would collide with the unique
  index. Better to leave the old identity in place than to fail the upgrade;
  `scripts/safe/shell` can then sort out the squatter by hand.

Revision ID: 0006_rename_probe_author
Revises: 0005_probe_author
Create Date: 2026-09-23 10:00:00.000000

"""

from alembic import op
import sqlalchemy as sa

from app.core.config import settings


# revision identifiers, used by Alembic.
revision = "0006_rename_probe_author"
down_revision = "0005_probe_author"
branch_labels = None
depends_on = None

# The identity 0005 minted before the rename. Pinned, not read from settings.
_OLD_EMAIL = "probes@poulse.example.com"
_OLD_USERNAME = "poulse"

_RENAME = sa.text(
    "UPDATE users SET email = :new_email, username = :new_username "
    "WHERE email = :old_email "
    "  AND is_active = false "
    "  AND NOT EXISTS (SELECT 1 FROM users WHERE email = :new_email) "
    "  AND NOT EXISTS (SELECT 1 FROM users WHERE lower(username) = lower(:new_username))"
)


def upgrade():
    op.get_bind().execute(
        _RENAME,
        {
            "old_email": _OLD_EMAIL,
            "new_email": settings.TRUST_PROBE_AUTHOR_EMAIL,
            "new_username": settings.TRUST_PROBE_AUTHOR_USERNAME,
        },
    )


def downgrade():
    op.get_bind().execute(
        _RENAME,
        {
            "old_email": settings.TRUST_PROBE_AUTHOR_EMAIL,
            "new_email": _OLD_EMAIL,
            "new_username": _OLD_USERNAME,
        },
    )
