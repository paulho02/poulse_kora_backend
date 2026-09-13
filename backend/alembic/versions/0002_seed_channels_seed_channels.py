# ruff: noqa: E501,I001
"""seed channels

Revision ID: 0002_seed_channels
Revises: 0001_initial_schema
Create Date: 2026-09-09 14:45:00.000000

"""
from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = '0002_seed_channels'
down_revision = '0001_initial_schema'
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
