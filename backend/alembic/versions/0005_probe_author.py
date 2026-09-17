# ruff: noqa: E501,I001
"""probe author

Mints the account Reviewer Trust publishes its test posts as (see
app/core/probes.py: `_ensure_probe_author`), so that it exists before anyone could
register its username or its email.

Until now the account was created on demand, on the first probe - which on a fresh
deployment is some time after launch. In between, both halves of its identity were
free for the taking: registering the username made the on-demand insert fail on the
unique index, so no probe was ever minted anywhere, silently; and registering the
email (email-validator accepts `example.com` subdomains) made an *active,
user-owned* account the probe author, with every test post in the deployment
published under that person's name and listed on their "Posted" screen. Minting the
row here closes both, because from then on the email is simply taken, and the
username is additionally reserved case-insensitively in `UserManager`.

Idempotent on the email, like 0002 on channel names. The password hash is of a
random value discarded on the spot, and `is_active` is false, so the row is an
identity and not a login; `_ensure_probe_author` now insists on that flag when it
looks the row up, so a squatted active account at this address can never be
picked up as the author by mistake.

Revision ID: 0005_probe_author
Revises: 0004_reviewer_trust
Create Date: 2026-09-16 10:00:00.000000

"""

import secrets
import uuid

from alembic import op
import sqlalchemy as sa
from fastapi_users.password import PasswordHelper

from app.core.config import settings


# revision identifiers, used by Alembic.
revision = "0005_probe_author"
down_revision = "0004_reviewer_trust"
branch_labels = None
depends_on = None


def upgrade():
    conn = op.get_bind()
    conn.execute(
        sa.text(
            "INSERT INTO users (id, email, hashed_password, is_active, is_superuser, "
            "is_verified, username, content_languages) "
            "VALUES (:id, :email, :hashed_password, false, false, true, :username, "
            ":content_languages) "
            "ON CONFLICT (email) DO NOTHING"
        ),
        {
            "id": uuid.uuid4(),
            "email": settings.TRUST_PROBE_AUTHOR_EMAIL,
            "hashed_password": PasswordHelper().hash(secrets.token_urlsafe(32)),
            "username": settings.TRUST_PROBE_AUTHOR_USERNAME,
            "content_languages": list(settings.CONTENT_LANGUAGES),
        },
    )


def downgrade():
    # Deliberately leaves the row in place. Once a single probe has been minted it
    # is referenced by `posts.author_id`, and a post is never deleted (see
    # app/core/probes.py on why probe rows are kept); before that, the row is
    # harmless. The pre-0005 code finds it by email and carries on.
    pass
