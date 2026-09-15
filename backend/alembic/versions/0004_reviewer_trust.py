# ruff: noqa: E501,I001
"""reviewer trust

Adds the storage Reviewer Trust needs: `posts.is_probe` (a test post - see
app/core/probes.py) and the `probe_responses` table that records how each one was
answered.

Nothing is backfilled and nothing needs to be. The score is computed over a trailing
window of rows, so on the deploy that runs this there are simply no probe rows yet,
every reader's probe component sits at neutral confidence, and everyone lands in the
normal band - which is exactly the standard fan-out they had the day before. Trust
becomes visible as probes accumulate, rather than arriving as a step change in
everybody's reach.

Unlike 0003, this needs no Redis rebuild: `trust:*` keys are caches and counters that
are created on first use and expire on their own.

`server_default='false'` on `posts.is_probe` is kept rather than dropped after the
backfill, which is the opposite of what 0003 did with `posts.language`. The reasoning is
the same in both cases - a default should exist only where "the caller forgot" and "the
caller meant it" have the same safe answer. For `language` they did not (forgetting
would have claimed the widest audience); here they do: a row inserted without this
column is an ordinary post, which is the truth for every writer in the codebase except
the one that sets it explicitly.

Revision ID: 0004_reviewer_trust
Revises: 0003_content_languages
Create Date: 2026-09-14 10:00:00.000000

"""
from alembic import op
import sqlalchemy as sa
from fastapi_users_db_sqlalchemy.generics import GUID


# revision identifiers, used by Alembic.
revision = '0004_reviewer_trust'
down_revision = '0003_content_languages'
branch_labels = None
depends_on = None


def upgrade():
    op.add_column(
        'posts',
        sa.Column(
            'is_probe',
            sa.Boolean(),
            nullable=False,
            server_default='false',
        ),
    )
    # Every query that touches is_probe filters on it *and* orders or bounds by created
    # (the pruning script's "probes older than X"), so the pair is one index rather than
    # two. Leading with the boolean is deliberate even though it has only two values:
    # probes are a small minority of the table, so the scan starts from the short side.
    op.create_index(
        'ix_posts_probe_created', 'posts', ['is_probe', 'created'], unique=False
    )

    op.create_table(
        'probe_responses',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('user_id', GUID(), nullable=False),
        sa.Column('post_id', sa.Integer(), nullable=False),
        sa.Column('variant_code', sa.String(length=64), nullable=False),
        sa.Column('expected_kind', sa.String(length=10), nullable=False),
        sa.Column('given_kind', sa.String(length=10), nullable=False),
        sa.Column('correct', sa.Boolean(), nullable=False),
        sa.Column(
            'created',
            sa.DateTime(timezone=True),
            server_default=sa.text('now()'),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(['post_id'], ['posts.id']),
        sa.ForeignKeyConstraint(['user_id'], ['users.id']),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('user_id', 'post_id', name='uq_probe_response_user_post'),
    )
    # The index the whole "no background job" decision rests on: scoring one reader is a
    # single range scan over their rows in the window, so it stays O(that reader's
    # probes) however large the table gets.
    op.create_index(
        'ix_probe_responses_user_created',
        'probe_responses',
        ['user_id', 'created'],
        unique=False,
    )


def downgrade():
    op.drop_index('ix_probe_responses_user_created', table_name='probe_responses')
    op.drop_table('probe_responses')
    op.drop_index('ix_posts_probe_created', table_name='posts')
    op.drop_column('posts', 'is_probe')
