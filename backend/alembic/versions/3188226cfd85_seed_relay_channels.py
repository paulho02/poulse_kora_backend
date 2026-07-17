# ruff: noqa: E501,I001
"""seed relay channels

Revision ID: 3188226cfd85
Revises: 418058cd4635
Create Date: 2026-07-16 20:46:48.561570

"""
from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = '3188226cfd85'
down_revision = '418058cd4635'
branch_labels = None
depends_on = None

CHANNELS = [
    {"name": "General", "color": "#6B7280", "description": "Everything and anything"},
    {"name": "Technology", "color": "#2563EB", "description": "Gadgets, software, and the future"},
    {"name": "Outdoors", "color": "#16A34A", "description": "Hiking, camping, and fresh air"},
    {"name": "Memes", "color": "#F59E0B", "description": "For the lulz"},
    {"name": "Politics", "color": "#DC2626", "description": "Debate responsibly"},
    {"name": "Local", "color": "#7C3AED", "description": "What's happening near you"},
]


def upgrade():
    # Idempotent so re-running `alembic upgrade head` against an already-seeded
    # database (e.g. a restored snapshot) is safe.
    conn = op.get_bind()
    for channel in CHANNELS:
        conn.execute(
            sa.text(
                "INSERT INTO channels (name, color, description) "
                "VALUES (:name, :color, :description) "
                "ON CONFLICT (name) DO NOTHING"
            ),
            channel,
        )


def downgrade():
    conn = op.get_bind()
    conn.execute(
        sa.text("DELETE FROM channels WHERE name IN :names").bindparams(
            sa.bindparam("names", expanding=True)
        ),
        {"names": [c["name"] for c in CHANNELS]},
    )
