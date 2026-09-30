# ruff: noqa: E501,I001
"""add posts.gifted_count - tokens gifted to a post's author, shown to the author

Backfilled from `post_reviews.gifted` (0008), so gifts made before this revision
are counted too: the column starts true rather than at zero.

Revision ID: 0009_post_gifted_count
Revises: 0008_review_gift
Create Date: 2026-09-30 12:00:00.000000

"""
from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = "0009_post_gifted_count"
down_revision = "0008_review_gift"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column(
        "posts",
        sa.Column("gifted_count", sa.Integer(), server_default="0", nullable=False),
    )
    op.execute(
        """
        UPDATE posts SET gifted_count = gifts.n
        FROM (
            SELECT post_id, count(*) AS n FROM post_reviews
            WHERE gifted GROUP BY post_id
        ) AS gifts
        WHERE posts.id = gifts.post_id
        """
    )


def downgrade():
    op.drop_column("posts", "gifted_count")
