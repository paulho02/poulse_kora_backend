# ruff: noqa: E501,I001
"""add post_reviews.gifted - a forward that hands its earned token to the author

Every existing review kept its token, so the server default `false` is the truth
for every row already there, not a placeholder.

Revision ID: 0008_review_gift
Revises: 0007_username_policy
Create Date: 2026-09-27 12:00:00.000000

"""
from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = "0008_review_gift"
down_revision = "0007_username_policy"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column(
        "post_reviews",
        sa.Column("gifted", sa.Boolean(), server_default="false", nullable=False),
    )


def downgrade():
    op.drop_column("post_reviews", "gifted")
