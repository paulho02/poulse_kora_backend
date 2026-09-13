# ruff: noqa: E501,I001
"""content languages

Adds the language axis of the feed's routing key: `posts.language` (what a post is
written in) and `users.content_languages` (what a reader accepts).

Both backfills are deliberately the *widest* value rather than a guess, so the deploy
changes nothing about who sees what:

- Existing posts become LANGUAGE_UNSPECIFIED. Their text was written before anyone was
  asked to declare a language, so any specific value would be invented - and "und"
  routes through the whole channel, which is exactly the audience those posts have
  been fanning out to all along. They keep circulating unchanged.
- Existing users accept every configured language, for the same reason: they never
  chose, so narrowing them would silently empty the feeds of people who did nothing.
  New accounts are narrowed to their request locale instead (see
  UserManager.on_after_register).

Redis is *not* touched here and this migration is not sufficient on its own: the
per-language audience sets do not exist until
`python -m scripts.dangerous.rebuild_redis` has run. Until then every reader is in the
plain channel set only, so UNSPECIFIED posts (which is all of them, immediately after
this) deliver normally and any newly created language-tagged post finds an empty
audience and parks for retry until the rebuild. Run the rebuild as part of the deploy.

Revision ID: 0003_content_languages
Revises: 0002_seed_channels
Create Date: 2026-09-09 16:20:00.000000

"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


# revision identifiers, used by Alembic.
revision = '0003_content_languages'
down_revision = '0002_seed_channels'
branch_labels = None
depends_on = None

# Hardcoded rather than read from Settings.CONTENT_LANGUAGES on purpose: a migration
# has to produce the same database whenever and wherever it is run, and a value read
# from the environment would make the backfill depend on the config of whoever happened
# to run it. If the deployment's language list has since grown, the extra languages are
# picked up by `rebuild_redis` and by users editing their own setting.
BACKFILL_LANGUAGES = ['en', 'de']


def upgrade():
    op.add_column(
        'posts',
        sa.Column(
            'language',
            sa.String(length=8),
            nullable=False,
            server_default='und',
        ),
    )
    op.add_column(
        'users',
        sa.Column(
            'content_languages',
            postgresql.ARRAY(sa.String(length=8)),
            nullable=True,
        ),
    )
    # Backfill before NOT NULL: the column has no server_default (the model supplies a
    # Python-side one), so existing rows would otherwise fail the constraint.
    op.execute(
        sa.text("UPDATE users SET content_languages = :langs").bindparams(
            sa.bindparam('langs', BACKFILL_LANGUAGES, type_=postgresql.ARRAY(sa.String(length=8)))
        )
    )
    op.alter_column('users', 'content_languages', nullable=False)
    # Dropped once the backfill is done: `posts.language` is always supplied by the
    # application, and leaving a default in place would let a future insert that forgot
    # the column quietly claim the widest audience there is.
    op.alter_column('posts', 'language', server_default=None)


def downgrade():
    op.drop_column('users', 'content_languages')
    op.drop_column('posts', 'language')
